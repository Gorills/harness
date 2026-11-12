// Adapted from sing-box's platform stub (GPL-3.0-or-later).
package main

/*
#include "bridge.h"
*/
import "C"
import (
	"context"
	"crypto/tls"
	stdjson "encoding/json"
	box "github.com/sagernet/sing-box"
	"github.com/sagernet/sing-box/adapter"
	"github.com/sagernet/sing-box/constant"
	"github.com/sagernet/sing-box/dns"
	"github.com/sagernet/sing-box/include"
	"github.com/sagernet/sing-box/option"
	tun "github.com/sagernet/sing-tun"
	"github.com/sagernet/sing/common/control"
	"github.com/sagernet/sing/common/json"
	"github.com/sagernet/sing/common/logger"
	M "github.com/sagernet/sing/common/metadata"
	"github.com/sagernet/sing/common/x/list"
	"github.com/sagernet/sing/service"
	"github.com/sagernet/sing/service/filemanager"
	"io"
	"net"
	"net/http"
	"net/netip"
	"os"
	"sync"
	"syscall"
	"time"
	"unsafe"
)

var mu sync.Mutex
var instance *box.Box

//export harness_start
func harness_start(fd C.int, raw *C.char, directory *C.char, network *C.char) *C.char {
	mu.Lock()
	defer mu.Unlock()
	if instance != nil {
		return C.CString("Клиент уже запущен")
	}
	content := C.GoString(raw)
	if len(content) > 65536 || fd < 0 {
		return C.CString("Некорректная конфигурация")
	}
	var info struct {
		Name      string
		Index     int
		MTU       int
		Addresses []string
	}
	if stdjson.Unmarshal([]byte(C.GoString(network)), &info) != nil || info.Name == "" || info.Index <= 0 {
		return C.CString("Нет доступной сети")
	}
	iface := control.Interface{Name: info.Name, Index: info.Index, MTU: info.MTU, Flags: net.FlagUp | net.FlagRunning}
	for _, a := range info.Addresses {
		p, err := netip.ParsePrefix(a)
		if err != nil {
			return C.CString("Некорректная сеть")
		}
		iface.Addresses = append(iface.Addresses, p)
	}
	ctx := box.Context(context.Background(), include.InboundRegistry(), include.OutboundRegistry(), include.EndpointRegistry(), include.DNSTransportRegistry(), include.ServiceRegistry(), include.CertificateProviderRegistry())
	ctx = filemanager.WithDefault(ctx, C.GoString(directory), C.GoString(directory), os.Getuid(), os.Getgid())
	platform := &phonePlatform{fd: int(fd), iface: iface}
	service.MustRegister[adapter.PlatformInterface](ctx, platform)
	options, err := json.UnmarshalExtendedContext[option.Options](ctx, []byte(content))
	if err != nil {
		return C.CString("Некорректная конфигурация туннеля")
	}
	client, err := box.New(box.Options{Context: ctx, Options: options})
	if err != nil {
		return C.CString("Не удалось создать клиент туннеля")
	}
	if err = client.Start(); err != nil {
		client.Close()
		return C.CString("Не удалось запустить туннель")
	}
	instance = client
	return nil
}

//export harness_probe
func harness_probe() C.int {
	mu.Lock()
	defer mu.Unlock()
	if instance == nil {
		return 0
	}
	outbound, ok := instance.Outbound().Outbound("proxy")
	if !ok {
		return 0
	}
	ctx, cancel := context.WithTimeout(context.Background(), 12*time.Second)
	defer cancel()
	transport := &http.Transport{TLSClientConfig: &tls.Config{MinVersion: tls.VersionTLS12}, DialContext: func(ctx context.Context, network, address string) (net.Conn, error) {
		return outbound.DialContext(ctx, network, M.ParseSocksaddr(address))
	}, DisableKeepAlives: true}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: 12 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	req, _ := http.NewRequestWithContext(ctx, "GET", "https://cp.cloudflare.com/generate_204", nil)
	response, err := client.Do(req)
	if err != nil {
		return 0
	}
	defer response.Body.Close()
	io.Copy(io.Discard, io.LimitReader(response.Body, 1024))
	if response.StatusCode == 204 {
		return 1
	}
	return 0
}

//export harness_stop
func harness_stop() {
	mu.Lock()
	defer mu.Unlock()
	if instance != nil {
		instance.Close()
		instance = nil
	}
}

//export harness_free
func harness_free(p *C.char) { C.free(unsafe.Pointer(p)) }
func main()                  {}

type phonePlatform struct {
	fd    int
	iface control.Interface
}

func (s *phonePlatform) Initialize(networkManager adapter.NetworkManager) error {
	return nil
}

func (s *phonePlatform) UsePlatformAutoDetectInterfaceControl() bool {
	return true
}

func (s *phonePlatform) AutoDetectInterfaceControl(fd int) error {
	if C.protect_socket(C.int(fd)) != 1 {
		return os.ErrPermission
	}
	return nil
}

func (s *phonePlatform) UsePlatformInterface() bool {
	return true
}

func (s *phonePlatform) OpenInterface(options *tun.Options, platformOptions option.TunPlatformOptions) (tun.Tun, error) {
	fd, err := syscall.Dup(s.fd)
	if err != nil {
		return nil, err
	}
	options.FileDescriptor = fd
	options.Name = "harness-phone"
	result, err := tun.New(*options)
	if err != nil {
		syscall.Close(fd)
	}
	return result, err
}

func (s *phonePlatform) ProcessPlatformOptions(options option.TunPlatformOptions) error {
	return nil
}

func (s *phonePlatform) UsePlatformDefaultInterfaceMonitor() bool {
	return true
}

func (s *phonePlatform) CreateDefaultInterfaceMonitor(logger logger.Logger) tun.DefaultInterfaceMonitor {
	return &phoneMonitor{iface: &s.iface}
}

func (s *phonePlatform) UsePlatformNetworkInterfaces() bool {
	return true
}

func (s *phonePlatform) NetworkInterfaces() ([]adapter.NetworkInterface, error) {
	return []adapter.NetworkInterface{{Interface: s.iface, Type: constant.InterfaceTypeOther}}, nil
}

func (s *phonePlatform) UnderNetworkExtension() bool {
	return false
}

func (s *phonePlatform) NetworkExtensionIncludeAllNetworks() bool {
	return false
}

func (s *phonePlatform) ClearDNSCache() {
}

func (s *phonePlatform) RequestPermissionForWIFIState() error {
	return nil
}

func (s *phonePlatform) UsePlatformWIFIMonitor() bool {
	return false
}

func (s *phonePlatform) ReadWIFIState(ctx context.Context) adapter.WIFIState {
	return adapter.WIFIState{}
}

func (s *phonePlatform) UsePlatformConnectionOwnerFinder() bool {
	return false
}

func (s *phonePlatform) FindConnectionOwner(request *adapter.FindConnectionOwnerRequest) (*adapter.ConnectionOwner, error) {
	return nil, os.ErrInvalid
}

func (s *phonePlatform) UsePlatformNotification() bool {
	return false
}

func (s *phonePlatform) SendNotification(notification *adapter.Notification) error {
	return nil
}

func (s *phonePlatform) CancelNotification(identifier string, typeID int32) error {
	return nil
}

func (s *phonePlatform) MyInterfaceAddress() []netip.Addr {
	return nil
}

func (s *phonePlatform) UsePlatformNeighborResolver() bool {
	return false
}

func (s *phonePlatform) StartNeighborMonitor(listener adapter.NeighborUpdateListener) error {
	return os.ErrInvalid
}

func (s *phonePlatform) CloseNeighborMonitor(listener adapter.NeighborUpdateListener) error {
	return nil
}

func (s *phonePlatform) UsePlatformShell() bool {
	return false
}

func (s *phonePlatform) CheckPlatformShell() error {
	return nil
}

func (s *phonePlatform) OpenShellSession(user *adapter.PlatformUser, command string, env []string, term string, rows int32, cols int32) (adapter.ShellSession, error) {
	return nil, os.ErrInvalid
}

func (s *phonePlatform) LookupSFTPServer() (string, error) {
	return "", os.ErrInvalid
}

func (s *phonePlatform) ReadSystemSSHHostKey() ([]byte, error) {
	return nil, os.ErrInvalid
}

func (s *phonePlatform) TailscaleHostname() string {
	return ""
}

func (s *phonePlatform) UsePlatformBridge() bool {
	return false
}

func (s *phonePlatform) CreateBridge(options adapter.BridgeOptions) (adapter.BridgeSession, error) {
	return nil, os.ErrInvalid
}

func (s *phonePlatform) LookupUser(username string) (*adapter.PlatformUser, error) {
	return nil, os.ErrInvalid
}

func (s *phonePlatform) UsePlatformLocalDNSTransport() bool {
	return false
}

func (s *phonePlatform) LocalDNSTransport() dns.TransportConstructorFunc[option.LocalDNSServerOptions] {
	return nil
}

type phoneMonitor struct {
	iface     *control.Interface
	callbacks list.List[tun.DefaultInterfaceUpdateCallback]
	names     []string
}

func (s *phoneMonitor) Start() error {
	return nil
}

func (s *phoneMonitor) Close() error {
	return nil
}

func (s *phoneMonitor) DefaultInterface() *control.Interface {
	return s.iface
}

func (s *phoneMonitor) OverrideAndroidVPN() bool {
	return false
}

func (s *phoneMonitor) AndroidVPNEnabled() bool {
	return false
}

func (s *phoneMonitor) RegisterCallback(callback tun.DefaultInterfaceUpdateCallback) *list.Element[tun.DefaultInterfaceUpdateCallback] {
	return s.callbacks.PushBack(callback)
}

func (s *phoneMonitor) UnregisterCallback(element *list.Element[tun.DefaultInterfaceUpdateCallback]) {
	s.callbacks.Remove(element)
}

func (s *phoneMonitor) RegisterMyInterface(interfaceName string) {
	s.names = append(s.names, interfaceName)
}

func (s *phoneMonitor) MyInterfaces() []string {
	return s.names
}
