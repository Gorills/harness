package android.window;
public class OnBackInvokedDispatcher {
    public static final int PRIORITY_DEFAULT=0;
    public OnBackInvokedCallback callback;
    public int registrations,unregistrations;
    public void registerOnBackInvokedCallback(int priority,OnBackInvokedCallback cb){if(callback!=null)throw new AssertionError("duplicate registration");callback=cb;registrations++;}
    public void unregisterOnBackInvokedCallback(OnBackInvokedCallback cb){if(callback!=cb)throw new AssertionError("unknown callback");callback=null;unregistrations++;}
}
