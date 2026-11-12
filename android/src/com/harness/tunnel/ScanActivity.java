package com.harness.tunnel;
import android.Manifest;
import android.app.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.hardware.Camera;
import android.os.Bundle;
import android.view.*;
import android.widget.*;
import com.google.zxing.*;
import com.google.zxing.common.HybridBinarizer;
import java.util.*;

@SuppressWarnings("deprecation")
public final class ScanActivity extends Activity implements SurfaceHolder.Callback,Camera.PreviewCallback {
    private SurfaceView view;private Camera camera;private boolean done=false,decoding=false;private SurfaceHolder holder;
    @Override public void onCreate(Bundle b){super.onCreate(b);getWindow().setFlags(WindowManager.LayoutParams.FLAG_SECURE,WindowManager.LayoutParams.FLAG_SECURE);LinearLayout root=new LinearLayout(this);root.setOrientation(1);root.setPadding(24,24,24,24);root.setBackgroundColor(0xff0b1220);TextView title=new TextView(this);title.setText("Сканировать QR-код\n\nОткройте «Добавить телефон» в туннелях на компьютере и наведите камеру на весь код.");title.setTextColor(0xfff1f5ff);title.setTextSize(18);root.addView(title);view=new SurfaceView(this);root.addView(view,new LinearLayout.LayoutParams(-1,0,1));Button close=new Button(this);close.setText("Отмена");close.setOnClickListener(v->finish());root.addView(close);setContentView(root);view.getHolder().addCallback(this);if(checkSelfPermission(Manifest.permission.CAMERA)!=PackageManager.PERMISSION_GRANTED)requestPermissions(new String[]{Manifest.permission.CAMERA},10);}
    @Override public void surfaceCreated(SurfaceHolder h){holder=h;open();}
    private void open(){if(holder==null||camera!=null||checkSelfPermission(Manifest.permission.CAMERA)!=PackageManager.PERMISSION_GRANTED)return;try{camera=Camera.open();Camera.Parameters p=camera.getParameters();List<Camera.Size> sizes=p.getSupportedPreviewSizes();Camera.Size chosen=sizes.get(0);for(Camera.Size size:sizes)if(size.width<=1280&&size.height<=960&&size.width*size.height>chosen.width*chosen.height||chosen.width>1280)chosen=size;p.setPreviewSize(chosen.width,chosen.height);if(p.getSupportedFocusModes().contains(Camera.Parameters.FOCUS_MODE_CONTINUOUS_PICTURE))p.setFocusMode(Camera.Parameters.FOCUS_MODE_CONTINUOUS_PICTURE);camera.setParameters(p);camera.setDisplayOrientation(90);camera.setPreviewDisplay(holder);camera.setPreviewCallback(this);camera.startPreview();}catch(Exception e){release();error("Камера недоступна. Проверьте разрешение и попробуйте снова");}}
    private void release(){if(camera!=null){camera.setPreviewCallback(null);camera.stopPreview();camera.release();camera=null;}}
    private void error(String text){new AlertDialog.Builder(this).setMessage(text).setPositiveButton("Закрыть",(d,w)->finish()).setOnCancelListener(d->finish()).show();}
    @Override public void onPreviewFrame(byte[] data,Camera source){if(done||decoding)return;decoding=true;try{Camera.Size size=source.getParameters().getPreviewSize();PlanarYUVLuminanceSource image=new PlanarYUVLuminanceSource(data,size.width,size.height,0,0,size.width,size.height,false);QRCodeReaderResult(image);}catch(Exception ignored){}finally{decoding=false;}}
    private void QRCodeReaderResult(LuminanceSource image)throws Exception{
        com.google.zxing.qrcode.QRCodeReader reader=new com.google.zxing.qrcode.QRCodeReader();Map<DecodeHintType,Object> hints=new EnumMap<>(DecodeHintType.class);hints.put(DecodeHintType.TRY_HARDER,Boolean.TRUE);Result result=reader.decode(new BinaryBitmap(new HybridBinarizer(image)),hints);String text=result.getText();if(text.length()>2800)return;done=true;release();setResult(RESULT_OK,new Intent().putExtra("qr",text));finish();
    }
    @Override public void surfaceChanged(SurfaceHolder h,int format,int w,int height){}
    @Override public void surfaceDestroyed(SurfaceHolder h){holder=null;release();}
    @Override protected void onPause(){release();super.onPause();}
    @Override protected void onResume(){super.onResume();open();}
    @Override public void onRequestPermissionsResult(int r,String[] permissions,int[] grants){super.onRequestPermissionsResult(r,permissions,grants);if(r==10){if(grants.length>0&&grants[0]==PackageManager.PERMISSION_GRANTED)open();else error("Без доступа к камере нельзя отсканировать профиль. Разрешение можно включить в настройках Android");}}
}
