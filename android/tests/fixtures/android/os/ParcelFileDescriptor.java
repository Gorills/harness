package android.os;
import java.io.*;
public class ParcelFileDescriptor implements AutoCloseable {
    public boolean closed;
    public int getFd(){return 42;}
    public static ParcelFileDescriptor fromFd(int fd){return new ParcelFileDescriptor();}
    public FileDescriptor getFileDescriptor(){return new FileDescriptor();}
    public void close(){closed=true;}
}
