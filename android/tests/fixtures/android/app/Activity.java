package android.app;
import android.os.Build;
import android.window.OnBackInvokedDispatcher;
public class Activity {
    public final OnBackInvokedDispatcher dispatcher=new OnBackInvokedDispatcher();
    public OnBackInvokedDispatcher getOnBackInvokedDispatcher(){if(Build.VERSION.SDK_INT<33)throw new AssertionError("API33 used on old Android");return dispatcher;}
}
