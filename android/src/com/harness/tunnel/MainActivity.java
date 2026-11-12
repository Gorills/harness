package com.harness.tunnel;

import android.Manifest;
import android.app.*;
import android.content.*;
import android.content.pm.*;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.VpnService;
import android.os.*;
import android.text.*;
import android.view.*;
import android.widget.*;
import java.util.*;

public final class MainActivity extends Activity {
    private static final int BG=0xff0b1220,CARD=0xff152137,TEXT=0xfff1f5ff,MUTED=0xffa5b4ce,BLUE=0xff5385ff,GREEN=0xff45dfb0;
    private LinearLayout body,rootLayout;
    private ProfileStore store;
    private BackNavigation backNavigation;
    private String page="home";
    private Button power;
    private TextView status,description,appCount;
    private final Handler handler=new Handler(Looper.getMainLooper());
    private final Runnable refresh=new Runnable(){public void run(){if(page.equals("home"))updateHome();handler.postDelayed(this,750);}};
    @Override public void onCreate(Bundle saved){super.onCreate(saved);getWindow().setStatusBarColor(BG);getWindow().setNavigationBarColor(BG);store=new ProfileStore(this);backNavigation=new BackNavigation(this,this::home);home();}
    @Override public void onResume(){super.onResume();handler.post(refresh);}
    @Override public void onPause(){handler.removeCallbacks(refresh);super.onPause();}
    @Override public void onDestroy(){backNavigation.close();super.onDestroy();}
    private int dp(float n){return (int)(getResources().getDisplayMetrics().density*n+.5f);}
    private GradientDrawable shape(int color,int radius){GradientDrawable s=new GradientDrawable();s.setColor(color);s.setCornerRadius(dp(radius));return s;}
    private LinearLayout column(){LinearLayout l=new LinearLayout(this);l.setOrientation(LinearLayout.VERTICAL);return l;}
    private TextView text(String s,int size,int color){TextView v=new TextView(this);v.setText(s);v.setTextSize(size);v.setTextColor(color);v.setPadding(0,dp(5),0,dp(5));return v;}
    private Button button(String title,Runnable action){Button b=new Button(this);b.setText(title);b.setAllCaps(false);b.setTextColor(TEXT);b.setTextSize(16);b.setBackground(shape(CARD,16));b.setMinHeight(dp(54));b.setPadding(dp(14),dp(10),dp(14),dp(10));b.setOnClickListener(v->action.run());LinearLayout.LayoutParams p=new LinearLayout.LayoutParams(-1,-2);p.topMargin=dp(10);b.setLayoutParams(p);return b;}
    private void base(String title,boolean back){
        backNavigation.setEnabled(back);
        LinearLayout outer=column();rootLayout=outer;outer.setBackgroundColor(BG);outer.setFitsSystemWindows(true);outer.setPadding(dp(24),dp(12),dp(24),dp(16));
        LinearLayout header=new LinearLayout(this);header.setGravity(Gravity.CENTER_VERTICAL);
        if(back){Button b=button("‹",this::home);b.setContentDescription("Назад на главный экран");header.addView(b,new LinearLayout.LayoutParams(dp(48),dp(48)));}
        TextView h=text(title,25,TEXT);h.setTypeface(null,Typeface.BOLD);header.addView(h,new LinearLayout.LayoutParams(0,dp(58),1));
        if(!back){Button b=button("⚙",this::settings);b.setContentDescription("Настройки");header.addView(b,new LinearLayout.LayoutParams(dp(48),dp(48)));}
        outer.addView(header);ScrollView scroll=new ScrollView(this);scroll.setFillViewport(true);body=column();body.setPadding(0,dp(12),0,dp(12));scroll.addView(body);outer.addView(scroll,new LinearLayout.LayoutParams(-1,0,1));setContentView(outer);
    }
    private String profileName(){try{Profile p=store.load();return p==null?"Добавьте подключение":p.name();}catch(Exception e){return "Профиль недоступен — отсканируйте QR заново";}}
    private void home(){
        page="home";base("Туннели",false);
        TextView eyebrow=text("HARNESS · ANDROID",11,MUTED);eyebrow.setLetterSpacing(.15f);body.addView(eyebrow);
        TextView name=text(profileName(),23,TEXT);name.setTypeface(null,Typeface.BOLD);body.addView(name);
        status=text("",16,GREEN);status.setGravity(Gravity.CENTER);LinearLayout.LayoutParams sp=new LinearLayout.LayoutParams(-1,-2);sp.topMargin=dp(24);status.setLayoutParams(sp);body.addView(status);
        FrameLayout holder=new FrameLayout(this);holder.setPadding(dp(12),dp(18),dp(12),dp(18));power=button("",this::toggle);power.setTextSize(26);power.setGravity(Gravity.CENTER);power.setTypeface(null,Typeface.BOLD);power.setElevation(dp(12));
        FrameLayout.LayoutParams bp=new FrameLayout.LayoutParams(dp(220),dp(220),Gravity.CENTER);holder.addView(power,bp);body.addView(holder,new LinearLayout.LayoutParams(-1,dp(260)));
        description=text("",14,MUTED);description.setGravity(Gravity.CENTER);description.setMinHeight(dp(68));body.addView(description);
        Button apps=button("",this::apps);appCount=apps;body.addView(apps);
        body.addView(button("Сканировать QR-код",this::scan));
        TextView note=text("Через VPS идут только выбранные приложения. Остальные используют обычный интернет.",13,MUTED);note.setGravity(Gravity.CENTER);note.setPadding(dp(8),dp(16),dp(8),dp(4));body.addView(note);updateHome();
    }
    private void updateHome(){if(power==null)return;boolean active=TunnelService.desired,connected=TunnelService.state.equals("connected"),pending=TunnelService.state.equals("stopping");
        status.setText(connected?"● Подключено":active?"● Подключение…":TunnelService.state.equals("error")?"● Не удалось подключиться":"○ Туннель выключен");status.setTextColor(connected?GREEN:active?BLUE:MUTED);
        GradientDrawable bg=new GradientDrawable(GradientDrawable.Orientation.TL_BR,connected?new int[]{0xff1c826e,0xff115448}:active?new int[]{0xff284b8f,0xff152b52}:new int[]{0xff243a62,0xff18243b});bg.setCornerRadius(dp(120));bg.setStroke(dp(connected?4:2),connected?GREEN:BLUE);power.setBackground(bg);power.setElevation(dp(connected?22:8));
        power.setText(pending?"…\nОтключаем":active?"⏻\nОтключить":"⏻\nПодключить");power.setContentDescription(active?"Отключить туннель":"Подключить туннель");power.setEnabled(!pending);
        description.setText(TunnelService.message);appCount.setText("Приложения через туннель  ·  "+store.installedSelection().size()+"  ›");
    }
    private void toggle(){if(TunnelService.desired){startService(new Intent(this,TunnelService.class).setAction(TunnelService.STOP));return;}
        try{if(store.load()==null){notice("Сначала отсканируйте QR-код в разделе «Туннели» на компьютере");return;}}catch(Exception e){notice("Профиль повреждён. Отсканируйте QR-код заново");return;}
        if(store.installedSelection().isEmpty()){notice("Выберите хотя бы одно приложение для туннеля");apps();return;}
        Intent permission=VpnService.prepare(this);if(permission!=null)startActivityForResult(permission,20);else startTunnel();
    }
    private void startTunnel(){if(Build.VERSION.SDK_INT>=33&&checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)!=PackageManager.PERMISSION_GRANTED)requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS},31);startForegroundService(new Intent(this,TunnelService.class).setAction(TunnelService.CONNECT));}
    private void settings(){page="settings";base("Настройки",true);body.addView(text("Подключение",15,MUTED));body.addView(text(profileName(),22,TEXT));body.addView(text("Общий доступ к VPS. Компьютер может быть выключен.",14,MUTED));body.addView(button("Добавить или заменить QR-профиль",this::scan));body.addView(button("Приложения через туннель",this::apps));body.addView(text("Встроены Hysteria2 и REALITY. Приложение проверяет сервер и подбирает рабочее соединение автоматически.",14,MUTED));
        body.addView(button("Удалить профиль с телефона",()->new AlertDialog.Builder(this).setTitle("Удалить профиль?").setMessage("Туннель на телефоне будет отключён. Сохранённый QR-код продолжит давать общий доступ к VPS.").setNegativeButton("Отмена",null).setPositiveButton("Удалить",(d,w)->{if(TunnelService.desired)startService(new Intent(this,TunnelService.class).setAction(TunnelService.STOP));try{store.clear();settings();}catch(Exception e){notice("Не удалось удалить профиль");}}).show()));body.addView(text("Harness Туннели · 0.1\nКлючи профиля защищены Android Keystore. Приложение не ведёт историю посещений.",12,MUTED));
    }
    private static final class AppRow{ApplicationInfo info;String name;AppRow(ApplicationInfo i,String n){info=i;name=n;}}
    private void apps(){page="apps";base("Приложения",true);body.addView(text("Отметьте приложения для туннеля",20,TEXT));body.addView(text("Неотмеченные приложения работают напрямую. Изменения применятся после сохранения.",14,MUTED));
        Set<String> selected=store.selection();EditText search=new EditText(this);search.setSingleLine(true);search.setTextColor(TEXT);search.setHintTextColor(MUTED);search.setHint("Поиск приложений");search.setTextSize(16);search.setPadding(dp(14),dp(12),dp(14),dp(12));search.setBackground(shape(CARD,12));body.addView(search,new LinearLayout.LayoutParams(-1,dp(54)));
        TextView count=text("",14,GREEN);body.addView(count);
        LinearLayout list=column();body.addView(list);
        List<AppRow> rows=new ArrayList<>();PackageManager pm=getPackageManager();for(ApplicationInfo info:pm.getInstalledApplications(0))if(info.enabled&&!info.packageName.equals(getPackageName())&&pm.checkPermission(Manifest.permission.INTERNET,info.packageName)==PackageManager.PERMISSION_GRANTED)rows.add(new AppRow(info,info.loadLabel(pm).toString()));rows.sort((a,b)->a.name.compareToIgnoreCase(b.name));
        Runnable render=()->{list.removeAllViews();String query=search.getText().toString().toLowerCase(Locale.ROOT);count.setText("Выбрано: "+selected.size());int visible=0;
            for(AppRow row:rows){if(!query.isEmpty()&&!row.name.toLowerCase(Locale.ROOT).contains(query)&&!row.info.packageName.toLowerCase(Locale.ROOT).contains(query))continue;visible++;
                LinearLayout item=new LinearLayout(this);item.setGravity(Gravity.CENTER_VERTICAL);item.setPadding(0,dp(10),0,dp(10));
                ImageView icon=new ImageView(this);try{icon.setImageDrawable(row.info.loadIcon(pm));}catch(Exception ignored){}item.addView(icon,new LinearLayout.LayoutParams(dp(38),dp(38)));
                LinearLayout label=column();label.setPadding(dp(12),0,dp(8),0);label.addView(text(row.name,15,TEXT));label.addView(text(row.info.packageName,11,MUTED));item.addView(label,new LinearLayout.LayoutParams(0,-2,1));
                CheckBox check=new CheckBox(this);check.setButtonTintList(android.content.res.ColorStateList.valueOf(BLUE));check.setContentDescription(row.name+" через туннель");check.setChecked(selected.contains(row.info.packageName));check.setOnCheckedChangeListener((b,value)->{if(value)selected.add(row.info.packageName);else selected.remove(row.info.packageName);count.setText("Выбрано: "+selected.size());});item.addView(check,new LinearLayout.LayoutParams(dp(52),dp(52)));item.setOnClickListener(v->check.setChecked(!check.isChecked()));list.addView(item);}
            if(visible==0)list.addView(text("Приложения не найдены",16,MUTED));};
        search.addTextChangedListener(new TextWatcher(){public void beforeTextChanged(CharSequence s,int a,int c,int f){}public void onTextChanged(CharSequence s,int st,int before,int count){render.run();}public void afterTextChanged(Editable e){}});
        render.run();rootLayout.addView(button("Сохранить выбор",()->{try{store.selection(selected);if(TunnelService.desired){if(store.installedSelection().isEmpty())startService(new Intent(this,TunnelService.class).setAction(TunnelService.STOP));else startTunnel();}home();}catch(Exception e){notice("Не удалось сохранить выбор приложений");}}));
    }
    private void scan(){if(TunnelService.desired){notice("Сначала отключите туннель на телефоне, чтобы заменить профиль");return;}startActivityForResult(new Intent(this,ScanActivity.class),21);}
    @Override protected void onActivityResult(int request,int result,Intent data){super.onActivityResult(request,result,data);if(request==20){if(result==RESULT_OK)startTunnel();else notice("Android не разрешил VPN. Разрешение можно запросить повторно кнопкой подключения");}else if(request==21&&result==RESULT_OK&&data!=null){try{Profile p=Profile.decode(data.getStringExtra("qr"));new AlertDialog.Builder(this).setTitle("Добавить «"+p.name()+"»?").setMessage("Вы получите общий доступ к этому VPS. Выберите приложения для туннеля после добавления.").setNegativeButton("Отмена",null).setPositiveButton("Добавить",(d,w)->{try{store.save(p);home();notice("Профиль добавлен. Выберите приложения для туннеля");}catch(Exception e){notice("Не удалось защитить и сохранить профиль");}}).show();}catch(Exception e){notice("Это не QR-профиль Harness или он повреждён");}}}
    private void notice(String message){new AlertDialog.Builder(this).setMessage(message).setPositiveButton("Понятно",null).show();}
    @Override public void onBackPressed(){if(!page.equals("home"))home();else super.onBackPressed();}
}
