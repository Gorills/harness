package com.harness.tunnel;

import android.content.Intent;
import android.net.*;
import android.os.ParcelFileDescriptor;
import java.lang.reflect.Field;
import java.util.*;
import java.util.concurrent.*;

/** Production TunnelService is compiled unchanged; only Android/native boundaries are stubs. */
public final class ServiceRegressionFixture {
    private static final class Job implements ScheduledFuture<Object> {
        final Runnable action;
        boolean cancelled,done;
        Job(Runnable runnable){action=runnable;}
        public long getDelay(TimeUnit unit){return 0;}
        public int compareTo(Delayed other){return 0;}
        public boolean cancel(boolean interrupt){cancelled=true;return true;}
        public boolean isCancelled(){return cancelled;}
        public boolean isDone(){return done||cancelled;}
        public Object get(){throw new UnsupportedOperationException();}
        public Object get(long timeout,TimeUnit unit){throw new UnsupportedOperationException();}
        void fire(){check(!isDone(),"only a pending timer may fire");done=true;action.run();}
        // Simulate a stale task already dequeued when its future was cancelled.
        void fireStale(){action.run();}
    }
    private static final class Worker extends ScheduledThreadPoolExecutor {
        final Deque<Runnable> immediate=new ArrayDeque<>();
        final List<Job> timers=new ArrayList<>();
        Worker(){super(1);}
        @Override public void execute(Runnable action){immediate.add(action);}
        @Override public ScheduledFuture<?> schedule(Runnable action,long delay,TimeUnit unit){Job job=new Job(action);timers.add(job);return job;}
        void drain(){while(!immediate.isEmpty())immediate.remove().run();}
        int pending(){int count=0;for(Job job:timers)if(!job.isDone())count++;return count;}
        Job next(){for(Job job:timers)if(!job.isDone())return job;throw new AssertionError("missing timer");}
    }
    private static final class Harness implements AutoCloseable {
        final TunnelService service=new TunnelService();
        final Worker worker=new Worker();
        Harness(Network active,Network... networks)throws Exception{
            ConnectivityManager.active=active;ConnectivityManager.networks=networks;ConnectivityManager.unregistered=0;
            NativeBridge.starts=0;NativeBridge.stops=0;NativeBridge.failed.clear();NativeBridge.healthy=true;VpnService.allowed=0;
            Field field=TunnelService.class.getDeclaredField("worker");field.setAccessible(true);((ScheduledThreadPoolExecutor)field.get(service)).shutdownNow();field.set(service,worker);service.onCreate();
        }
        void start(){service.onStartCommand(new Intent(service,TunnelService.class).setAction(TunnelService.CONNECT),0,1);worker.drain();check(VpnService.allowed==1,"selected application allowlist remains explicit");}
        void capabilities(Network network){ConnectivityManager.callback.onCapabilitiesChanged(network,new NetworkCapabilities(network));}
        void link(Network network){ConnectivityManager.callback.onLinkPropertiesChanged(network,network.link);}
        public void close(){service.onDestroy();worker.drain();worker.shutdownNow();}
    }
    public static void main(String[] args)throws Exception{
        staleRetryAndCallbackCoalescing();networkSelectionAndChanges();failedDefaultUsesAlternative();failedRoundRetriesAlternatives();
    }
    private static void staleRetryAndCallbackCoalescing()throws Exception{
        Network cell=new Network("cellular",true);
        try(Harness h=new Harness(cell,cell)){
            NativeBridge.healthy=false;h.start();ParcelFileDescriptor tun=VpnService.established;
            check(TunnelService.state.equals("retrying")&&h.worker.pending()==1,"failure owns one retry");check(!tun.closed,"failure retains blocking TUN");Job stale=h.worker.next();
            NativeBridge.healthy=true;cell.link=new LinkProperties(1);
            for(int i=0;i<100;i++){h.capabilities(cell);h.link(cell);}
            check(h.worker.immediate.size()==1,"callback burst queues one recovery notification");h.worker.drain();int starts=NativeBridge.starts;
            check(TunnelService.state.equals("connected")&&h.worker.pending()==1,"early recovery leaves one health chain: state="+TunnelService.state+" starts="+starts+" timers="+h.worker.pending());
            stale.fireStale();check(NativeBridge.starts==starts&&h.worker.pending()==1,"stale retry cannot restart healthy engine or add timers");
            for(int i=0;i<100;i++){h.capabilities(cell);h.link(cell);}check(h.worker.immediate.size()==1,"repeated callback queue is bounded");h.worker.drain();
            check(NativeBridge.starts==starts,"unchanged path does not restart healthy engine");
            for(int i=0;i<5;i++){h.worker.next().fire();check(h.worker.pending()==1,"health ticks retain exactly one chain");}
            Job oldHealth=h.worker.next();h.link(cell);h.service.onStartCommand(new Intent(h.service,TunnelService.class).setAction(TunnelService.STOP),0,2);oldHealth.fireStale();h.worker.drain();
            check(NativeBridge.starts==starts&&h.worker.pending()==0&&tun.closed,"stop invalidates timers and callbacks and closes TUN");check(ConnectivityManager.unregistered==2,"stop releases both callback registrations");
            System.out.println("A1 fail → early recovery → stale retry; 200-event bursts; health/stop ownership: PASS");
        }
    }
    private static void networkSelectionAndChanges()throws Exception{
        Network wifi=new Network("captive-wifi",false),cell=new Network("default-cellular",true);wifi.captive=true;
        try(Harness h=new Harness(cell,wifi,cell)){
            h.start();ParcelFileDescriptor tun=VpnService.established;check(NativeBridge.current==cell,"validated default wins over first captive network");
            wifi.captive=false;wifi.validated=true;ConnectivityManager.active=wifi;
            ConnectivityManager.defaultCallback.onCapabilitiesChanged(wifi,new NetworkCapabilities(wifi));h.worker.drain();check(NativeBridge.current==wifi,"default callback switches to validated Wi-Fi");
            int starts=NativeBridge.starts;wifi.link=new LinkProperties(2);h.link(wifi);h.worker.drain();check(NativeBridge.starts==starts+1,"link change restarts against current interface properties");
            wifi.validated=false;wifi.captive=true;h.capabilities(wifi);h.worker.drain();check(NativeBridge.current==cell,"capability loss selects validated alternative");
            ConnectivityManager.networks=new Network[]{wifi};ConnectivityManager.active=wifi;ConnectivityManager.callback.onLost(cell);h.worker.drain();
            check(TunnelService.state.equals("retrying")&&h.service.underlying.length==0&&!tun.closed,"underlay loss retains TUN without binding unusable link");
            wifi.validated=true;wifi.captive=false;h.capabilities(wifi);h.worker.drain();check(TunnelService.state.equals("connected")&&NativeBridge.current==wifi&&h.worker.pending()==1,"capability recovery supersedes loss retry");
            System.out.println("A3 captive/default/validation/link/loss/recovery selection and TUN retention: PASS");
        }
    }
    private static void failedDefaultUsesAlternative()throws Exception{
        Network wifi=new Network("validated-but-VPS-blocked-default",true),cell=new Network("working-cellular",true);
        try(Harness h=new Harness(wifi,wifi,cell)){
            NativeBridge.failed.add(wifi);h.start();check(TunnelService.state.equals("retrying"),"default VPS probe fails");h.worker.next().fire();
            check(NativeBridge.current==cell&&TunnelService.state.equals("connected"),"retry uses alternative after both transports fail on default");int starts=NativeBridge.starts;
            h.capabilities(wifi);h.worker.drain();check(NativeBridge.starts==starts,"unchanged failed default does not displace healthy alternative");
            NativeBridge.failed.remove(wifi);wifi.link=new LinkProperties(1);h.link(wifi);h.worker.drain();check(NativeBridge.current==wifi,"changed default path becomes eligible again");
            System.out.println("A3 failed default → alternative; unchanged failed link retained; changed link readmitted: PASS");
        }
    }
    private static void failedRoundRetriesAlternatives()throws Exception{
        Network wifi=new Network("wifi",true),cell=new Network("cell",true);
        try(Harness h=new Harness(wifi,wifi,cell)){
            NativeBridge.failed.add(wifi);NativeBridge.failed.add(cell);h.start();h.worker.next().fire();check(NativeBridge.current==cell,"second carrier attempted");
            h.worker.next().fire();check(NativeBridge.current==wifi,"exhausted round restarts with default");NativeBridge.failed.remove(cell);h.worker.next().fire();
            check(NativeBridge.current==cell&&TunnelService.state.equals("connected"),"later retry revisits recovered alternative without platform capability change");
            System.out.println("A3 exhausted carrier round revisits alternatives without network change: PASS");
        }
    }
    private static void check(boolean condition,String message){if(!condition)throw new AssertionError(message);}
}
