package fakeip

import (
	"context"
	"errors"
	"github.com/sagernet/sing-box/adapter"
	"net/netip"
	"testing"
)

type failingStorage struct {
	*MemoryStorage
	fail bool
}

func (s *failingStorage) FakeIPStoreAndMetadata(address netip.Addr, domain string, metadata *adapter.FakeIPMetadata) error {
	if s.fail {
		return errors.New("synthetic storage failure")
	}
	return s.MemoryStorage.FakeIPStoreAndMetadata(address, domain, metadata)
}
func TestFailedPersistenceDoesNotExposeOrAdvanceAllocation(t *testing.T) {
	pool := netip.MustParsePrefix("198.18.0.0/15")
	s := NewStore(context.Background(), nil, pool, netip.Prefix{})
	s.inet4Current = pool.Addr().Next()
	storage := &failingStorage{NewMemoryStorage(), true}
	s.storage = storage
	previous := s.inet4Current
	if address, err := s.Create("fixture.example", false); err == nil || address.IsValid() {
		t.Fatal("uncommitted address exposed", address, err)
	}
	if s.inet4Current != previous {
		t.Fatal("failed allocation advanced cursor")
	}
	storage.fail = false
	address, err := s.Create("fixture.example", false)
	if err != nil || address != previous.Next() {
		t.Fatal("retry did not recover", address, err)
	}
}
