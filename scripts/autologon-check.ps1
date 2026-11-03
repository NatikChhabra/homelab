# Reports only WHETHER Winlogon's DefaultPassword LSA secret exists (never its value).
# Must run as SYSTEM. Output: logs\autologon-check.txt
Add-Type @'
using System; using System.Runtime.InteropServices;
public static class L {
  [StructLayout(LayoutKind.Sequential)] public struct U { public ushort Len, Max; public IntPtr Buf; }
  [StructLayout(LayoutKind.Sequential)] public struct O { public int Len; public IntPtr a,b; public int c; public IntPtr d,e; }
  [DllImport("advapi32.dll")] public static extern uint LsaOpenPolicy(IntPtr s, ref O o, int acc, out IntPtr h);
  [DllImport("advapi32.dll")] public static extern uint LsaRetrievePrivateData(IntPtr h, ref U k, out IntPtr d);
  [DllImport("advapi32.dll")] public static extern uint LsaFreeMemory(IntPtr p);
  [DllImport("advapi32.dll")] public static extern uint LsaClose(IntPtr h);
  public static string Check() {
    var o = new O(); IntPtr h; uint r = LsaOpenPolicy(IntPtr.Zero, ref o, 0x4, out h); if (r != 0) return "open failed " + r;
    string n = "DefaultPassword"; var k = new U(); k.Buf = Marshal.StringToHGlobalUni(n); k.Len = (ushort)(n.Length*2); k.Max = (ushort)(k.Len+2);
    IntPtr d; r = LsaRetrievePrivateData(h, ref k, out d); string res;
    if (r != 0) res = "absent (status " + r + ")"; else { var u = (U)Marshal.PtrToStructure(d, typeof(U)); res = u.Len > 0 ? "present" : "empty"; LsaFreeMemory(d); }
    LsaClose(h); return res;
  }
}
'@
"$(Get-Date -Format s) DefaultPassword secret: $([L]::Check())" | Set-Content C:\ServerData\Stacks\logs\autologon-check.txt
