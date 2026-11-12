package android.net;

public class ConnectivityManager {
    public static Network[] networks;
    public static Network active;
    public static NetworkCallback callback,defaultCallback;
    public static int unregistered;
    public Network[] getAllNetworks(){return networks;}
    public Network getActiveNetwork(){return active;}
    public NetworkCapabilities getNetworkCapabilities(Network n){
        for(Network candidate:networks)if(candidate==n)return new NetworkCapabilities(n);
        return null;
    }
    public LinkProperties getLinkProperties(Network n){return getNetworkCapabilities(n)==null?null:n.link;}
    public void registerNetworkCallback(NetworkRequest request,NetworkCallback cb){callback=cb;}
    public void registerDefaultNetworkCallback(NetworkCallback cb){defaultCallback=cb;}
    public void unregisterNetworkCallback(NetworkCallback cb){unregistered++;if(callback==cb)callback=null;if(defaultCallback==cb)defaultCallback=null;}
    public static class NetworkCallback {
        public void onAvailable(Network n){}
        public void onLost(Network n){}
        public void onCapabilitiesChanged(Network n,NetworkCapabilities caps){}
        public void onLinkPropertiesChanged(Network n,LinkProperties link){}
    }
}
