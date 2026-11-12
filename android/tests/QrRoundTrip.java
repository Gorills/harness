import com.google.zxing.BinaryBitmap;
import com.google.zxing.LuminanceSource;
import com.google.zxing.common.HybridBinarizer;
import com.google.zxing.qrcode.QRCodeReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.List;

/** Real encoder/decoder interoperability, without Android stubs or camera claims. */
public final class QrRoundTrip {
  private static final class Pixels extends LuminanceSource {
    private final byte[] matrix;
    Pixels(int width, byte[] matrix) {
      super(width, width);
      this.matrix = matrix;
    }
    @Override public byte[] getMatrix() { return matrix; }
    @Override public byte[] getRow(int y, byte[] row) {
      if (row == null || row.length < getWidth()) row = new byte[getWidth()];
      System.arraycopy(matrix, y * getWidth(), row, 0, getWidth());
      return row;
    }
  }
  public static void main(String[] args) throws Exception {
    List<String> rows = Files.readAllLines(Paths.get(args[0]), StandardCharsets.US_ASCII);
    int width = rows.size();
    byte[] pixels = new byte[width * width];
    for (int y = 0; y < width; y++) {
      if (rows.get(y).length() != width) throw new AssertionError("Non-square QR");
      for (int x = 0; x < width; x++) {
        pixels[y * width + x] = rows.get(y).charAt(x) == '1' ? 0 : (byte) 255;
      }
    }
    String decoded = new QRCodeReader().decode(
        new BinaryBitmap(new HybridBinarizer(new Pixels(width, pixels)))).getText();
    String expected = new String(Files.readAllBytes(Paths.get(args[1])), StandardCharsets.UTF_8);
    if (!expected.equals(decoded)) throw new AssertionError("QR payload changed");
    System.out.println("QR roundtrip passed: " + decoded.length() + " characters");
  }
}
