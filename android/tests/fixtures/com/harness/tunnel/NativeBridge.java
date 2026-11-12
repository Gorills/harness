package com.harness.tunnel;
import android.net.Network;
import java.util.*;
final class NativeBridge {
    static boolean healthy;
    static int starts,stops;
    static Network current;
    static final Set<Network> failed=new HashSet<>();
    static String start(int fd,String config,String dir,String info,TunnelService service){starts++;current=service.underlying[0];return null;}
    static boolean probe(){return healthy&&!failed.contains(current);}
    static void stop(){stops++;}
}
