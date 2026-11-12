package android.net;

public class NetworkCapabilities {
    public static final int NET_CAPABILITY_INTERNET=1,NET_CAPABILITY_NOT_VPN=2,NET_CAPABILITY_VALIDATED=3,NET_CAPABILITY_CAPTIVE_PORTAL=4;
    private final Network network;
    public NetworkCapabilities(Network n){network=n;}
    public boolean hasCapability(int cap){switch(cap){case NET_CAPABILITY_INTERNET:return network.internet;case NET_CAPABILITY_NOT_VPN:return network.nonVpn;case NET_CAPABILITY_VALIDATED:return network.validated;case NET_CAPABILITY_CAPTIVE_PORTAL:return network.captive;default:return false;}}
}
