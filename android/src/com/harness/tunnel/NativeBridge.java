package com.harness.tunnel;
final class NativeBridge {
    static { System.loadLibrary("harnessbox"); }
    static native String start(int fd, String config, String directory, String network, TunnelService owner);
    static native boolean probe();
    static native void stop();
}
