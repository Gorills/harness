package android.net;
import android.content.Intent;
import android.app.*;
import android.os.*;
import java.io.File;
public class VpnService {
    public static final String CONNECTIVITY_SERVICE="conn";
    public static final int START_NOT_STICKY=2,STOP_FOREGROUND_REMOVE=1;
    public Network[] underlying=new Network[0];
    public static ParcelFileDescriptor established;
    public static int allowed;
    public Object getSystemService(String name){return new ConnectivityManager();}
    public <T> T getSystemService(Class<T> type){return type.cast(new NotificationManager());}
    public String getPackageName(){return "com.harness.tunnel";}
    public void sendBroadcast(Intent intent){}
    public File getNoBackupFilesDir(){return new File("/tmp");}
    public static Intent prepare(VpnService service){return null;}
    public boolean protect(int fd){return true;}
    public boolean setUnderlyingNetworks(Network[] networks){underlying=networks;return true;}
    public void onCreate(){}
    public int onStartCommand(Intent intent,int flags,int id){return 0;}
    public void startForeground(int id,Notification notification){}
    public void stopForeground(int flags){}
    public void stopSelf(){}
    public void onRevoke(){}
    public void onDestroy(){}
    public static class Builder {
        public Builder setSession(String name){return this;}
        public Builder setMtu(int mtu){return this;}
        public Builder addAddress(String address,int prefix){return this;}
        public Builder addRoute(String address,int prefix){return this;}
        public Builder addDnsServer(String address){return this;}
        public Builder addAllowedApplication(String name)throws android.content.pm.PackageManager.NameNotFoundException{allowed++;return this;}
        public Builder setBlocking(boolean blocking){return this;}
        public ParcelFileDescriptor establish(){established=new ParcelFileDescriptor();return established;}
    }
}
