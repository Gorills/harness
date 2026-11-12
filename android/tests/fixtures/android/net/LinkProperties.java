package android.net;
public class LinkProperties {
    private final int version;
    public LinkProperties(int v){version=v;}
    public String getInterfaceName(){return "lo";}
    public int getMtu(){return 1500;}
    public java.util.List<LinkAddress> getLinkAddresses(){return java.util.Arrays.asList(new LinkAddress());}
    @Override public boolean equals(Object other){return other instanceof LinkProperties&&version==((LinkProperties)other).version;}
    @Override public int hashCode(){return version;}
}
