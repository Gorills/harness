package com.harness.tunnel;

import android.app.Activity;
import android.os.Build;
import android.window.OnBackInvokedCallback;
import android.window.OnBackInvokedDispatcher;

/** Keep API33 classes out of the legacy navigation path on Android8–12. */
final class BackNavigation {
    private final PlatformBack platform;
    BackNavigation(Activity activity,Runnable home){platform=Build.VERSION.SDK_INT>=33?new PlatformBack(activity,home):null;}
    void setEnabled(boolean enabled){if(platform!=null)platform.setEnabled(enabled);}
    void close(){setEnabled(false);}

    private static final class PlatformBack {
        private final OnBackInvokedDispatcher dispatcher;
        private final OnBackInvokedCallback callback;
        private boolean registered;
        PlatformBack(Activity activity,Runnable home){dispatcher=activity.getOnBackInvokedDispatcher();callback=home::run;}
        void setEnabled(boolean enabled){
            if(enabled==registered)return;
            if(enabled)dispatcher.registerOnBackInvokedCallback(OnBackInvokedDispatcher.PRIORITY_DEFAULT,callback);
            else dispatcher.unregisterOnBackInvokedCallback(callback);
            registered=enabled;
        }
    }
}
