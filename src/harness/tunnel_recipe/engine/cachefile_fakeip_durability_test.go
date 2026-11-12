package cachefile

import (
	"github.com/sagernet/bbolt"
	"github.com/sagernet/sing-box/adapter"
	"net/netip"
	"path/filepath"
	"testing"
)

func TestFakeIPAllocationMetadataRemainsAfterReads(t *testing.T) {
	path := filepath.Join(t.TempDir(), "cache.db")
	db, err := bbolt.Open(path, 0600, nil)
	if err != nil {
		t.Fatal(err)
	}
	c := &CacheFile{DB: db}
	address := netip.MustParseAddr("198.18.0.2")
	metadata := &adapter.FakeIPMetadata{Inet4Range: netip.MustParsePrefix("198.18.0.0/15"), Inet4Current: address}
	if err := c.FakeIPStoreAndMetadata(address, "fixture.example", metadata); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 3; i++ {
		got := c.FakeIPMetadata()
		if got == nil || *got != *metadata {
			t.Fatal("metadata read consumed committed cursor", got)
		}
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}
	db, err = bbolt.Open(path, 0600, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	c = &CacheFile{DB: db}
	if got, ok := c.FakeIPLoad(address); !ok || got != "fixture.example" {
		t.Fatal("reverse mapping missing", got, ok)
	}
	if got, ok := c.FakeIPLoadDomain("fixture.example", false); !ok || got != address {
		t.Fatal("domain index missing", got, ok)
	}
	if got := c.FakeIPMetadata(); got == nil || *got != *metadata {
		t.Fatal("cursor not committed with mapping", got)
	}
}
