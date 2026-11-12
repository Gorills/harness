package com.harness.tunnel;

import android.app.*;
import android.content.*;
import android.net.*;
import android.os.*;
import org.json.*;
import java.net.NetworkInterface;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicBoolean;

public final class TunnelService extends VpnService {
    public static final String CONNECT="com.harness.tunnel.CONNECT",STOP="com.harness.tunnel.STOP",CHANGED="com.harness.tunnel.STATE";
    public static volatile String state="off",message="Готов к подключению";
    public static volatile boolean desired=false;
    private final ScheduledThreadPoolExecutor worker=new ScheduledThreadPoolExecutor(1);
    private final AtomicInteger generation=new AtomicInteger();
    // Owned by worker: one retry or health timer; epochs also invalidate already-dequeued work.
    private ScheduledFuture<?> scheduled;
    private long scheduleEpoch;
    private ParcelFileDescriptor tun;
    private volatile Network underlay;
    private ConnectivityManager connectivity;
    private ConnectivityManager.NetworkCallback callback,defaultCallback;
    private final Map<Network,UnderlayState> failedUnderlays=new HashMap<>();
    private UnderlayState appliedUnderlay;
    private boolean nativeActive=false;
    private int transport=0;
    @Override public void onCreate(){super.onCreate();worker.setRemoveOnCancelPolicy(true);connectivity=(ConnectivityManager)getSystemService(CONNECTIVITY_SERVICE);NotificationManager nm=getSystemService(NotificationManager.class);nm.createNotificationChannel(new NotificationChannel("tunnel","Подключение к туннелю",NotificationManager.IMPORTANCE_LOW));}
    @Override public int onStartCommand(Intent intent,int flags,int startId){
        if(intent==null||STOP.equals(intent.getAction())){disconnect();return START_NOT_STICKY;}
        if(!CONNECT.equals(intent.getAction())){stopSelf();return START_NOT_STICKY;}
        desired=true;int gen=generation.incrementAndGet();update("connecting","Подключаемся к VPS…");startForeground(1,notification());
        worker.execute(()->connect(gen));return START_NOT_STICKY;
    }
    private Notification notification(){
        Intent launch=new Intent(this,MainActivity.class);PendingIntent open=PendingIntent.getActivity(this,0,launch,PendingIntent.FLAG_IMMUTABLE|PendingIntent.FLAG_UPDATE_CURRENT);
        PendingIntent off=PendingIntent.getService(this,1,new Intent(this,TunnelService.class).setAction(STOP),PendingIntent.FLAG_IMMUTABLE|PendingIntent.FLAG_UPDATE_CURRENT);
        return new Notification.Builder(this,"tunnel").setSmallIcon(R.drawable.ic_tunnel).setContentTitle("Harness · Туннели").setContentText(message).setContentIntent(open).setOngoing(true).addAction(new Notification.Action.Builder(null,"Отключить",off).build()).build();
    }
    private void update(String next,String text){state=next;message=text;Intent i=new Intent(CHANGED).setPackage(getPackageName());sendBroadcast(i);if(desired)getSystemService(NotificationManager.class).notify(1,notification());}
    private boolean current(int gen){return desired&&generation.get()==gen;}
    private void closeNative(){if(nativeActive){NativeBridge.stop();nativeActive=false;}}
    private void cancelScheduled(){scheduleEpoch++;if(scheduled!=null){scheduled.cancel(false);scheduled=null;}}
    private void schedule(int gen,boolean recovery,int seconds){
        cancelScheduled();long epoch=scheduleEpoch;
        scheduled=worker.schedule(()->{if(!current(gen)||epoch!=scheduleEpoch)return;scheduled=null;if(recovery)recover(gen);else tick(gen);},seconds,TimeUnit.SECONDS);
    }
    private void cleanup(){cancelScheduled();closeNative();if(tun!=null){try{tun.close();}catch(Exception ignored){}tun=null;}if(callback!=null){try{connectivity.unregisterNetworkCallback(callback);}catch(Exception ignored){}callback=null;}if(defaultCallback!=null){try{connectivity.unregisterNetworkCallback(defaultCallback);}catch(Exception ignored){}defaultCallback=null;}underlay=null;appliedUnderlay=null;failedUnderlays.clear();}
    private static final class UnderlayState{
        final boolean usable,validated;final LinkProperties link;
        UnderlayState(NetworkCapabilities caps,LinkProperties properties){usable=caps!=null&&caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)&&caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_VPN)&&!caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_CAPTIVE_PORTAL)&&properties!=null&&properties.getInterfaceName()!=null;validated=caps!=null&&caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED);link=properties;}
        boolean samePath(UnderlayState other){return other!=null&&usable==other.usable&&validated==other.validated&&Objects.equals(link,other.link);}
    }
    private UnderlayState networkState(Network network){return new UnderlayState(connectivity.getNetworkCapabilities(network),connectivity.getLinkProperties(network));}
    private Network chooseNetwork(){
        Network active=connectivity.getActiveNetwork(),best=null,fallback=null;int bestRank=5,fallbackRank=5;
        Set<Network> networks=new LinkedHashSet<>();if(active!=null)networks.add(active);Collections.addAll(networks,connectivity.getAllNetworks());
        failedUnderlays.keySet().retainAll(networks);
        for(Network n:networks){UnderlayState candidate=networkState(n);if(!candidate.usable)continue;
            int rank=(candidate.validated?0:2)+(Objects.equals(active,n)?0:1);
            if(rank<fallbackRank){fallback=n;fallbackRank=rank;}
            if(!candidate.samePath(failedUnderlays.get(n))&&rank<bestRank){best=n;bestRank=rank;}
        }
        // Try another carrier after a failed probe, then retry the preferred path once all failed.
        return best!=null?best:fallback;
    }
    private ConnectivityManager.NetworkCallback networkCallback(int gen,AtomicBoolean queued){
        return new ConnectivityManager.NetworkCallback(){
            private void changed(){
                if(!current(gen)||!queued.compareAndSet(false,true))return;
                try{worker.execute(()->{queued.set(false);if(!current(gen))return;Network next=chooseNetwork();UnderlayState nextState=next==null?null:networkState(next);if(!Objects.equals(next,underlay)||(nextState!=null&&!nextState.samePath(appliedUnderlay)))recover(gen);});}catch(RejectedExecutionException ignored){queued.set(false);}
            }
            @Override public void onAvailable(Network n){/* Capabilities and link properties follow on API26+. */}
            @Override public void onLost(Network n){changed();}
            @Override public void onCapabilitiesChanged(Network n,NetworkCapabilities caps){changed();}
            @Override public void onLinkPropertiesChanged(Network n,LinkProperties link){changed();}
        };
    }
    private String networkInfo()throws Exception{
        LinkProperties link=connectivity.getLinkProperties(underlay);if(link==null||link.getInterfaceName()==null)throw new Exception();
        NetworkInterface iface=NetworkInterface.getByName(link.getInterfaceName());if(iface==null)throw new Exception();JSONArray addresses=new JSONArray();for(LinkAddress a:link.getLinkAddresses())addresses.put(a.toString());
        return new JSONObject().put("Name",link.getInterfaceName()).put("Index",iface.getIndex()).put("MTU",Math.max(1280,link.getMtu())).put("Addresses",addresses).toString();
    }
    // Called on Go-created threads. Both TCP and UDP sockets must be protected.
    public boolean protectSocket(int fd){
        Network network=underlay;if(network==null||!protect(fd))return false;
        try(ParcelFileDescriptor duplicate=ParcelFileDescriptor.fromFd(fd)){network.bindSocket(duplicate.getFileDescriptor());return true;}catch(Exception e){return false;}
    }
    private void connect(int gen){
        if(!current(gen))return;cleanup();
        try{
            ProfileStore store=new ProfileStore(this);Profile profile=store.load();Set<String> packages=store.installedSelection();
            if(profile==null)throw new UserError("Сначала отсканируйте QR-код в Harness");
            if(packages.isEmpty())throw new UserError("Выберите хотя бы одно установленное приложение");
            if(VpnService.prepare(this)!=null)throw new UserError("Разрешите VPN в системном окне Android");
            Builder builder=new Builder().setSession("Harness · "+profile.name()).setMtu(1400).addAddress("172.19.0.1",30).addAddress("fdfe:dcba:9876::1",126).addRoute("0.0.0.0",0).addRoute("::",0).addDnsServer("172.19.0.2");
            // Never establish unless at least one valid addAllowedApplication succeeded.
            int added=0;for(String name:packages){try{builder.addAllowedApplication(name);added++;}catch(android.content.pm.PackageManager.NameNotFoundException ignored){}}
            if(added==0)throw new UserError("Выбранные приложения удалены. Выберите приложения заново");
            builder.setBlocking(false);tun=builder.establish();if(tun==null)throw new UserError("Android не разрешил создать VPN");
            AtomicBoolean queued=new AtomicBoolean();callback=networkCallback(gen,queued);defaultCallback=networkCallback(gen,queued);
            connectivity.registerNetworkCallback(new NetworkRequest.Builder().addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET).addCapability(NetworkCapabilities.NET_CAPABILITY_NOT_VPN).build(),callback);
            connectivity.registerDefaultNetworkCallback(defaultCallback);
            recover(gen);
        }catch(UserError e){fatal(e.getMessage());}catch(Exception|LinkageError e){fatal("Не удалось запустить VPN. Проверьте профиль и разрешение Android");}
    }
    private void recover(int gen){
        if(!current(gen))return;cancelScheduled();closeNative();
        try{
            Profile profile=new ProfileStore(this).load();if(profile==null||tun==null)throw new Exception();
            underlay=chooseNetwork();setUnderlyingNetworks(underlay==null?new Network[0]:new Network[]{underlay});if(underlay==null)throw new Exception();appliedUnderlay=networkState(underlay);if(appliedUnderlay.samePath(failedUnderlays.get(underlay)))failedUnderlays.clear();
            update("connecting","Проверяем соединение с VPS…");
            for(int i=0;i<2&&current(gen);i++){
                String error=NativeBridge.start(tun.getFd(),profile.config(i==0),getNoBackupFilesDir().getAbsolutePath(),networkInfo(),this);
                if(error==null){nativeActive=true;if(NativeBridge.probe()&&current(gen)){transport=i;failedUnderlays.remove(underlay);update("connected","Выбранные приложения используют туннель");schedule(gen,false,20);return;}closeNative();}
            }
        }catch(Exception|LinkageError ignored){}
        if(current(gen)){if(underlay!=null&&appliedUnderlay!=null)failedUnderlays.put(underlay,appliedUnderlay);update("retrying","VPS недоступен. Повторяем подключение; выбранные приложения ожидают VPN");schedule(gen,true,10);}
    }
    private void tick(int gen){
        if(!current(gen))return;
        if(nativeActive&&NativeBridge.probe()){schedule(gen,false,20);}else recover(gen);
    }
    private void fatal(String error){desired=false;generation.incrementAndGet();cleanup();update("error",error);stopForeground(STOP_FOREGROUND_REMOVE);stopSelf();}
    private void disconnect(){desired=false;generation.incrementAndGet();update("stopping","Отключаем…");worker.execute(()->{cleanup();update("off","Туннель отключён");stopForeground(STOP_FOREGROUND_REMOVE);stopSelf();});}
    @Override public void onRevoke(){disconnect();super.onRevoke();}
    @Override public void onDestroy(){desired=false;generation.incrementAndGet();worker.execute(this::cleanup);worker.shutdown();super.onDestroy();}
    static final class UserError extends Exception{UserError(String text){super(text);}}
}
