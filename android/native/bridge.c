#include <jni.h>
#include <stdlib.h>
#include "bridge.h"

extern char *harness_start(int, char *, char *, char *);
extern void harness_stop(void);
extern int harness_probe(void);
extern void harness_free(char *);
static JavaVM *vm;
static jobject service;
static jmethodID protect_method;

int protect_socket(int fd) {
    JNIEnv *env = NULL;
    int attached = 0;
    if ((*vm)->GetEnv(vm, (void **)&env, JNI_VERSION_1_6) == JNI_EDETACHED) {
        if ((*vm)->AttachCurrentThread(vm, &env, NULL) != JNI_OK) return 0;
        attached = 1;
    }
    if (!env) return 0;
    if (!service || !protect_method) { if (attached) (*vm)->DetachCurrentThread(vm); return 0; }
    jboolean result = (*env)->CallBooleanMethod(env, service, protect_method, fd);
    if ((*env)->ExceptionCheck(env)) { (*env)->ExceptionClear(env); result = JNI_FALSE; }
    if (attached) (*vm)->DetachCurrentThread(vm);
    return result == JNI_TRUE;
}
JNIEXPORT jstring JNICALL Java_com_harness_tunnel_NativeBridge_start(JNIEnv *env, jclass type, jint fd, jstring config, jstring dir, jstring network, jobject owner) {
    (void)type;
    (*env)->GetJavaVM(env, &vm);
    if (service) return (*env)->NewStringUTF(env, "Client already running");
    service = (*env)->NewGlobalRef(env, owner);
    jclass cls = (*env)->GetObjectClass(env, owner);
    protect_method = (*env)->GetMethodID(env, cls, "protectSocket", "(I)Z");
    (*env)->DeleteLocalRef(env, cls);
    if (!protect_method) { (*env)->ExceptionClear(env); (*env)->DeleteGlobalRef(env, service); service=NULL; return (*env)->NewStringUTF(env,"Socket protection unavailable"); }
    const char *c = (*env)->GetStringUTFChars(env,config,NULL);
    const char *d = (*env)->GetStringUTFChars(env,dir,NULL);
    const char *n = (*env)->GetStringUTFChars(env,network,NULL);
    char *error = harness_start(fd,(char*)c,(char*)d,(char*)n);
    (*env)->ReleaseStringUTFChars(env,config,c); (*env)->ReleaseStringUTFChars(env,dir,d); (*env)->ReleaseStringUTFChars(env,network,n);
    jstring result = error ? (*env)->NewStringUTF(env,error) : NULL;
    if(error) {harness_free(error);(*env)->DeleteGlobalRef(env,service);service=NULL;protect_method=NULL;}
    return result;
}
JNIEXPORT jboolean JNICALL Java_com_harness_tunnel_NativeBridge_probe(JNIEnv *env,jclass type){(void)env;(void)type;return harness_probe()==1?JNI_TRUE:JNI_FALSE;}
JNIEXPORT void JNICALL Java_com_harness_tunnel_NativeBridge_stop(JNIEnv *env,jclass type){(void)type;harness_stop();if(service){(*env)->DeleteGlobalRef(env,service);service=NULL;protect_method=NULL;}}
