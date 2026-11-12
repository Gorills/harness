package com.harness.tunnel;

import android.app.Activity;
import android.os.Build;

public final class BackNavigationFixture {
    public static void main(String[] args){
        for(int sdk:new int[]{26,32,33,36}){
            Build.VERSION.SDK_INT=sdk;Activity activity=new Activity();int[] homes={0};BackNavigation[] navigation=new BackNavigation[1];
            navigation[0]=new BackNavigation(activity,()->{homes[0]++;navigation[0].setEnabled(false);});
            navigation[0].setEnabled(false);
            check(activity.dispatcher.callback==null,"Home must retain platform task Back");
            navigation[0].setEnabled(true);navigation[0].setEnabled(true);
            if(sdk>=33){
                check(activity.dispatcher.registrations==1,"internal pages register once");activity.dispatcher.callback.onBackInvoked();
                check(homes[0]==1&&activity.dispatcher.callback==null,"Back returns to Home and releases interception");
                navigation[0].setEnabled(true);navigation[0].close();
                check(activity.dispatcher.callback==null&&activity.dispatcher.unregistrations==2,"destroy unregisters");
            }else check(activity.dispatcher.registrations==0,"legacy Android retains onBackPressed path");
            System.out.println("Back callback lifecycle API"+sdk+": PASS");
        }
    }
    private static void check(boolean condition,String message){if(!condition)throw new AssertionError(message);}
}
