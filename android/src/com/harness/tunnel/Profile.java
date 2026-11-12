package com.harness.tunnel;

import android.util.Base64;
import org.json.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.security.cert.CertificateFactory;
import java.io.ByteArrayInputStream;

final class Profile {
    final JSONObject data;
    Profile(JSONObject value) throws Exception {
        exact(value,"v","name","host","r","h");
        if (value.getInt("v") != 1 || !(value.get("v") instanceof Integer)) throw new Exception();
        text(value,"name",1,160);
        String host=text(value,"host",2,64);
        if (!host.matches("[0-9a-fA-F:.]+") || !host.contains(".") && !host.contains(":")) throw new Exception();
        java.net.InetAddress.getByName(host); // numeric-only input, never fetch a URL
        JSONObject r=value.getJSONObject("r"), h=value.getJSONObject("h");
        exact(r,"u","k","s","i"); exact(h,"p","o","s","c");
        if(!text(r,"u",36,36).matches("[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"))throw new Exception();
        if(!text(r,"k",43,43).matches("[A-Za-z0-9_-]{43}"))throw new Exception();
        if(!text(r,"i",0,16).matches("([0-9a-fA-F]{2}){0,8}"))throw new Exception();
        domain(text(r,"s",1,253)); domain(text(h,"s",1,253));
        for(String key:new String[]{"p","o"}) if(!text(h,key,1,256).matches("[!-~]+"))throw new Exception();
        String cert=text(h,"c",0,4096);
        if(!cert.isEmpty()) {
            if(!cert.startsWith("-----BEGIN CERTIFICATE-----") || cert.indexOf("-----BEGIN",1)>=0)throw new Exception();
            CertificateFactory.getInstance("X.509").generateCertificate(new ByteArrayInputStream(cert.getBytes(StandardCharsets.US_ASCII)));
        }
        data=value;
    }
    static Profile decode(String code) throws Exception {
        if(code==null || code.length()>2800 || !code.startsWith("HT1:") || !code.substring(4).matches("[A-Za-z0-9_-]+"))throw new Exception();
        byte[] bytes=Base64.decode(code.substring(4),Base64.URL_SAFE|Base64.NO_WRAP|Base64.NO_PADDING);
        if(bytes.length>2100)throw new Exception();
        String raw=new String(bytes,StandardCharsets.UTF_8);
        return new Profile(new JSONObject(raw));
    }
    static void exact(JSONObject object,String...keys)throws Exception {
        Set<String> allowed=new HashSet<>(Arrays.asList(keys));
        if(object.length()!=allowed.size())throw new Exception();
        Iterator<String> iterator=object.keys();while(iterator.hasNext())if(!allowed.contains(iterator.next()))throw new Exception();
    }
    static String text(JSONObject o,String key,int min,int max)throws Exception{
        Object v=o.get(key);if(!(v instanceof String))throw new Exception();String s=(String)v;
        if(s.length()<min||s.length()>max||s.indexOf('\0')>=0)throw new Exception();return s;
    }
    static void domain(String s)throws Exception{if(!s.matches("[A-Za-z0-9][A-Za-z0-9.-]{0,251}[A-Za-z0-9]")&&!s.matches("[A-Za-z0-9]"))throw new Exception();}
    String name(){return data.optString("name");}
    String config(boolean hysteria) throws Exception {
        JSONObject tls=new JSONObject().put("enabled",true);
        JSONObject out=new JSONObject().put("tag","proxy").put("server",data.getString("host")).put("server_port",443).put("connect_timeout","8s");
        if(hysteria){
            JSONObject h=data.getJSONObject("h");
            tls.put("server_name",h.getString("s"));
            if(!h.getString("c").isEmpty())tls.put("certificate",new JSONArray().put(h.getString("c")));
            out.put("type","hysteria2").put("password",h.getString("p")).put("obfs",new JSONObject().put("type","salamander").put("password",h.getString("o")));
        } else {
            JSONObject r=data.getJSONObject("r");
            tls.put("server_name",r.getString("s")).put("utls",new JSONObject().put("enabled",true).put("fingerprint","chrome"))
               .put("reality",new JSONObject().put("enabled",true).put("public_key",r.getString("k")).put("short_id",r.getString("i")));
            out.put("type","vless").put("uuid",r.getString("u")).put("flow","xtls-rprx-vision").put("packet_encoding","xudp");
        }
        out.put("tls",tls);
        JSONObject dns=new JSONObject().put("servers",new JSONArray().put(new JSONObject().put("type","https").put("tag","remote").put("server","1.1.1.1").put("path","/dns-query").put("tls",new JSONObject().put("server_name","cloudflare-dns.com")).put("detour","proxy")))
            .put("final","remote").put("strategy","prefer_ipv4");
        JSONObject tun=new JSONObject().put("type","tun").put("tag","tun-in").put("address",new JSONArray().put("172.19.0.1/30").put("fdfe:dcba:9876::1/126")).put("mtu",1400).put("auto_route",true).put("stack","gvisor");
        JSONObject route=new JSONObject().put("rules",new JSONArray().put(new JSONObject().put("port",53).put("action","hijack-dns"))).put("final","proxy").put("auto_detect_interface",true);
        return new JSONObject().put("log",new JSONObject().put("disabled",true)).put("dns",dns).put("inbounds",new JSONArray().put(tun)).put("outbounds",new JSONArray().put(out)).put("route",route).toString();
    }
}
