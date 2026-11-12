package android.net;
public class Network {
    public final String label;
    public boolean validated,internet=true,nonVpn=true,captive;
    public LinkProperties link=new LinkProperties(0);
    public Network(String name,boolean valid){label=name;validated=valid;}
    public void bindSocket(java.io.FileDescriptor fd){}
}
