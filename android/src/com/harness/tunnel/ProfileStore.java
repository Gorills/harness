package com.harness.tunnel;
import android.content.Context;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import java.io.*;
import java.nio.file.Files;
import java.security.KeyStore;
import java.util.*;
import javax.crypto.*;
import javax.crypto.spec.GCMParameterSpec;

final class ProfileStore {
    private static final String ALIAS="harness-tunnel-profile-v1";
    private final Context context;
    ProfileStore(Context context){this.context=context;}
    private javax.crypto.SecretKey key()throws Exception{
        KeyStore ks=KeyStore.getInstance("AndroidKeyStore");ks.load(null);
        if(ks.containsAlias(ALIAS))return (javax.crypto.SecretKey)ks.getKey(ALIAS,null);
        KeyGenerator gen=KeyGenerator.getInstance("AES","AndroidKeyStore");
        gen.init(new KeyGenParameterSpec.Builder(ALIAS,KeyProperties.PURPOSE_ENCRYPT|KeyProperties.PURPOSE_DECRYPT).setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build());return gen.generateKey();
    }
    private File file(){return new File(context.getNoBackupFilesDir(),"tunnel-profile.enc");}
    synchronized void save(Profile profile)throws Exception{
        Cipher cipher=Cipher.getInstance("AES/GCM/NoPadding");cipher.init(Cipher.ENCRYPT_MODE,key());
        byte[] encrypted=cipher.doFinal(profile.data.toString().getBytes(java.nio.charset.StandardCharsets.UTF_8));
        File next=new File(context.getNoBackupFilesDir(),"tunnel-profile.new");
        try(FileOutputStream stream=new FileOutputStream(next)){stream.write(cipher.getIV());stream.write(encrypted);stream.getFD().sync();}
        java.nio.file.Files.move(next.toPath(),file().toPath(),java.nio.file.StandardCopyOption.REPLACE_EXISTING,java.nio.file.StandardCopyOption.ATOMIC_MOVE);
    }
    synchronized Profile load()throws Exception{
        if(!file().exists())return null;
        if(file().length()<28||file().length()>8192)throw new IOException();
        byte[] raw=Files.readAllBytes(file().toPath());Cipher cipher=Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.DECRYPT_MODE,key(),new GCMParameterSpec(128,Arrays.copyOf(raw,12)));
        return new Profile(new org.json.JSONObject(new String(cipher.doFinal(Arrays.copyOfRange(raw,12,raw.length)),java.nio.charset.StandardCharsets.UTF_8)));
    }
    synchronized void clear()throws Exception{
        if(file().exists()&&!file().delete())throw new IOException();
        KeyStore ks=KeyStore.getInstance("AndroidKeyStore");ks.load(null);ks.deleteEntry(ALIAS);
    }
    Set<String> selection(){return new HashSet<>(context.getSharedPreferences("routing",0).getStringSet("packages",Collections.emptySet()));}
    void selection(Set<String> selected)throws IOException{
        if(!context.getSharedPreferences("routing",0).edit().putStringSet("packages",new HashSet<>(selected)).commit())throw new IOException();
    }
    Set<String> installedSelection(){
        Set<String> result=selection();result.remove(context.getPackageName());
        result.removeIf(name->{try{android.content.pm.ApplicationInfo i=context.getPackageManager().getApplicationInfo(name,0);return !i.enabled;}catch(Exception e){return true;}});return result;
    }
}
