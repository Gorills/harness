// Security regression tests adapted only to the pre-existing uTLS testConfig fixture.
// Source: Go CL724120, CL763767, CL804261 (BSD-3-Clause).
package tls

import (
	"context"
	"fmt"
	"io"
	"strings"
	"testing"
	"time"
)

func TestServerHelloTrailingMessage(t *testing.T) {
	// In TLS 1.3 the change cipher spec message is optional. If a CCS message
	// is not sent, after reading the ServerHello, the read traffic secret is
	// set, and all following messages must be encrypted. If the server sends
	// additional unencrypted messages in a record with the ServerHello, the
	// client must either fail or ignore the additional messages.

	c, s := localPipe(t)
	go func() {
		ctx := context.Background()
		srv := Server(s, testConfig)
		clientHello, _, err := srv.readClientHello(ctx)
		if err != nil {
			testFatal(t, err)
		}

		hs := serverHandshakeStateTLS13{
			c:           srv,
			ctx:         ctx,
			clientHello: clientHello,
		}
		if err := hs.processClientHello(); err != nil {
			testFatal(t, err)
		}
		if err := transcriptMsg(hs.clientHello, hs.transcript); err != nil {
			testFatal(t, err)
		}

		record, err := concatHandshakeMessages(hs.hello, &encryptedExtensionsMsg{alpnProtocol: "h2"})
		if err != nil {
			testFatal(t, err)
		}

		if _, err := s.Write(record); err != nil {
			testFatal(t, err)
		}
		srv.Close()
	}()

	cli := Client(c, testConfig)
	expectedErr := "tls: handshake buffer not empty before setting read traffic secret"
	if err := cli.Handshake(); err == nil {
		t.Fatal("expected error from incomplete handshake, got nil")
	} else if err.Error() != expectedErr {
		t.Fatalf("expected error %q, got %q", expectedErr, err.Error())
	}
}

func TestClientHelloTrailingMessage(t *testing.T) {
	// Same as TestServerHelloTrailingMessage but for the client side.

	c, s := localPipe(t)
	go func() {
		cli := Client(c, testConfig)

		hello, _, _, err := cli.makeClientHello()
		if err != nil {
			testFatal(t, err)
		}

		record, err := concatHandshakeMessages(hello, &certificateMsgTLS13{})
		if err != nil {
			testFatal(t, err)
		}

		if _, err := c.Write(record); err != nil {
			testFatal(t, err)
		}
		cli.Close()
	}()

	srv := Server(s, testConfig)
	expectedErr := "tls: handshake buffer not empty before setting read traffic secret"
	if err := srv.Handshake(); err == nil {
		t.Fatal("expected error from incomplete handshake, got nil")
	} else if err.Error() != expectedErr {
		t.Fatalf("expected error %q, got %q", expectedErr, err.Error())
	}
}

func TestDoubleClientHelloHRR(t *testing.T) {
	// If a client sends two ClientHello messages in a single record, and the
	// server sends a HRR after reading the first ClientHello, the server must
	// either fail or ignore the trailing ClientHello.

	c, s := localPipe(t)

	go func() {
		cli := Client(c, testConfig)

		hello, _, _, err := cli.makeClientHello()
		if err != nil {
			testFatal(t, err)
		}
		hello.keyShares = nil

		record, err := concatHandshakeMessages(hello, hello)
		if err != nil {
			testFatal(t, err)
		}

		if _, err := c.Write(record); err != nil {
			testFatal(t, err)
		}
		cli.Close()
	}()

	srv := Server(s, testConfig)
	expectedErr := "tls: handshake buffer not empty before HelloRetryRequest"
	if err := srv.Handshake(); err == nil {
		t.Fatal("expected error from incomplete handshake, got nil")
	} else if err.Error() != expectedErr {
		t.Fatalf("expected error %q, got %q", expectedErr, err.Error())
	}
}

// concatHandshakeMessages marshals and concatenates the given handshake
// messages into a single record.
func concatHandshakeMessages(msgs ...handshakeMessage) ([]byte, error) {
	var marshalled []byte
	for _, msg := range msgs {
		data, err := msg.marshal()
		if err != nil {
			return nil, err
		}
		marshalled = append(marshalled, data...)
	}
	m := len(marshalled)
	outBuf := make([]byte, recordHeaderLen)
	outBuf[0] = byte(recordTypeHandshake)
	vers := VersionTLS12
	outBuf[1] = byte(vers >> 8)
	outBuf[2] = byte(vers)
	outBuf[3] = byte(m >> 8)
	outBuf[4] = byte(m)
	outBuf = append(outBuf, marshalled...)
	return outBuf, nil
}

func TestMultipleKeyUpdate(t *testing.T) {
	for _, requestUpdate := range []bool{true, false} {
		t.Run(fmt.Sprintf("requestUpdate=%t", requestUpdate), func(t *testing.T) {

			c, s := localPipe(t)
			cfg := testConfig.Clone()
			cfg.MinVersion = VersionTLS13
			cfg.MaxVersion = VersionTLS13
			client := Client(c, testConfig)
			server := Server(s, testConfig)

			clientHandshakeDone := make(chan struct{})
			go func() {
				if err := client.Handshake(); err != nil {
				}
				close(clientHandshakeDone)
				io.Copy(io.Discard, server)
			}()

			if err := server.Handshake(); err != nil {
				t.Fatalf("server handshake failed: %v\n", err)
			}
			<-clientHandshakeDone

			c.SetReadDeadline(time.Now().Add(1 * time.Second))
			s.SetReadDeadline(time.Now().Add(1 * time.Second))

			kuMsg, err := (&keyUpdateMsg{updateRequested: requestUpdate}).marshal()
			if err != nil {
				t.Fatalf("failed to marshal key update message: %v", err)
			}

			client.out.Lock()
			if _, err := client.writeRecordLocked(recordTypeHandshake, append(kuMsg, kuMsg...)); err != nil {
				t.Fatalf("failed to write key update messages: %v", err)
			}
			client.out.Unlock()

			_, err = io.Copy(io.Discard, client)
			if err == nil {
				t.Fatal("expected multiple key update messages to cause an error, got nil")
			} else if !strings.HasSuffix(err.Error(), "tls: unexpected message") {
				t.Fatalf("unexpected error: %v", err)
			}
		})
	}
}

// TestKeyUpdateSpamPostHandshakeTLS13 tests that TLS 1.3 KeyUpdate messages do
// not count as a state-advancing message after a handshake has been completed.
func TestKeyUpdateSpamPostHandshakeTLS13(t *testing.T) {
	client, server := localPipe(t)
	defer server.Close()
	defer client.Close()

	go func() {
		c := Client(client, testConfig.Clone())
		if err := c.Handshake(); err != nil {
			t.Error(err)
			return
		}
		ku, err := (&keyUpdateMsg{}).marshal()
		if err != nil {
			t.Error(err)
			return
		}
		cs := cipherSuiteTLS13ByID(c.cipherSuite)
		for i := 0; i <= maxUselessRecords; i++ {
			c.writeRecordLocked(recordTypeHandshake, ku)
			c.setWriteTrafficSecret(cs, QUICEncryptionLevelInitial, cs.nextTrafficSecret(c.out.trafficSecret))
		}
	}()
	s := Server(server, testConfig.Clone())
	if err := s.Handshake(); err != nil {
		t.Fatal(err)
	}
	expectedErr := "tls: too many non-advancing records"
	if _, err := s.Read(make([]byte, 1)); err == nil || err.Error() != expectedErr {
		t.Fatalf("unexpected error: got %v, want %q", err, expectedErr)
	}
}

func TestUClientServerHelloTrailingMessage(t *testing.T) {
	// In TLS 1.3 the change cipher spec message is optional. If a CCS message
	// is not sent, after reading the ServerHello, the read traffic secret is
	// set, and all following messages must be encrypted. If the server sends
	// additional unencrypted messages in a record with the ServerHello, the
	// client must either fail or ignore the additional messages.

	c, s := localPipe(t)
	go func() {
		ctx := context.Background()
		srv := Server(s, testConfig)
		clientHello, _, err := srv.readClientHello(ctx)
		if err != nil {
			testFatal(t, err)
		}

		hs := serverHandshakeStateTLS13{
			c:           srv,
			ctx:         ctx,
			clientHello: clientHello,
		}
		if err := hs.processClientHello(); err != nil {
			testFatal(t, err)
		}
		if err := transcriptMsg(hs.clientHello, hs.transcript); err != nil {
			testFatal(t, err)
		}

		record, err := concatHandshakeMessages(hs.hello, &encryptedExtensionsMsg{alpnProtocol: "h2"})
		if err != nil {
			testFatal(t, err)
		}

		if _, err := s.Write(record); err != nil {
			testFatal(t, err)
		}
		srv.Close()
	}()

	cli := UClient(c, testConfig, HelloChrome_Auto)
	expectedErr := "tls: handshake buffer not empty before setting read traffic secret"
	if err := cli.Handshake(); err == nil {
		t.Fatal("expected error from incomplete handshake, got nil")
	} else if err.Error() != expectedErr {
		t.Fatalf("expected error %q, got %q", expectedErr, err.Error())
	}
}

func TestUClientMultipleKeyUpdate(t *testing.T) {
	for _, requestUpdate := range []bool{true, false} {
		t.Run(fmt.Sprintf("requestUpdate=%t", requestUpdate), func(t *testing.T) {

			c, s := localPipe(t)
			cfg := testConfig.Clone()
			cfg.MinVersion = VersionTLS13
			cfg.MaxVersion = VersionTLS13
			client := UClient(c, testConfig, HelloChrome_Auto)
			server := Server(s, testConfig)

			clientHandshakeDone := make(chan struct{})
			go func() {
				if err := client.Handshake(); err != nil {
				}
				close(clientHandshakeDone)
				io.Copy(io.Discard, server)
			}()

			if err := server.Handshake(); err != nil {
				t.Fatalf("server handshake failed: %v\n", err)
			}
			<-clientHandshakeDone

			c.SetReadDeadline(time.Now().Add(1 * time.Second))
			s.SetReadDeadline(time.Now().Add(1 * time.Second))

			kuMsg, err := (&keyUpdateMsg{updateRequested: requestUpdate}).marshal()
			if err != nil {
				t.Fatalf("failed to marshal key update message: %v", err)
			}

			client.out.Lock()
			if _, err := client.writeRecordLocked(recordTypeHandshake, append(kuMsg, kuMsg...)); err != nil {
				t.Fatalf("failed to write key update messages: %v", err)
			}
			client.out.Unlock()

			_, err = io.Copy(io.Discard, client)
			if err == nil {
				t.Fatal("expected multiple key update messages to cause an error, got nil")
			} else if !strings.HasSuffix(err.Error(), "tls: unexpected message") {
				t.Fatalf("unexpected error: %v", err)
			}
		})
	}
}
