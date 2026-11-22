"""Builds dist\\Duplikill\\Duplikill.exe, dist\\Duplikill-Portable-win64.zip, and dist\\Duplikill-Setup.exe.

Run from the project folder: .venv\\Scripts\\python.exe -I tools\\build_exe.py
"""
import glob
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYI = os.path.join(ROOT, ".venv", "Scripts", "pyinstaller.exe")
DIST = os.path.join(ROOT, "dist", "Duplikill")
ZIP_OUT = os.path.join(ROOT, "dist", "Duplikill-Portable-win64.zip")
SETUP_OUT = os.path.join(ROOT, "dist", "Duplikill-Setup.exe")
CZKAWKA_URL = "https://github.com/qarmin/czkawka/releases/download/12.0.2/windows_czkawka_cli.exe"
CSC = r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"

UNUSED = [  # relative to _internal\PySide6
    "opengl32sw.dll", "translations", "Qt6Quick.dll", "Qt6Qml.dll", "Qt6QmlModels.dll", "Qt6QmlMeta.dll",
    "Qt6QmlWorkerScript.dll", "Qt6Pdf.dll", "Qt6VirtualKeyboard.dll", "Qt6Network.dll", "QtNetwork.pyd",
    "Qt6OpenGL.dll", "plugins/platforminputcontexts", "plugins/networkinformation", "plugins/tls",
    "plugins/imageformats/qpdf.dll", "plugins/generic",
]

INSTALLER_CS = r"""
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

[assembly: AssemblyTitle("Duplikill Setup")]
[assembly: AssemblyProduct("Duplikill")]
[assembly: AssemblyVersion("0.1.0.0")]

class SetupForm : Form {
    TextBox pathBox;
    CheckBox cbDesktop, cbStartMenu, cbLaunch;
    Button btnInstall;
    Label statusLbl;
    ProgressBar bar;

    public SetupForm() {
        Text = "Duplikill Setup";
        ClientSize = new Size(520, 310);
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        StartPosition = FormStartPosition.CenterScreen;
        BackColor = Color.FromArgb(14, 14, 14);
        ForeColor = Color.White;
        Font = new Font("Consolas", 10f, FontStyle.Bold);
        try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch {}

        Label title = new Label {
            Text = "**** DUPLIKILL SETUP ****",
            Location = new Point(24, 20),
            Size = new Size(472, 28),
            Font = new Font("Consolas", 14f, FontStyle.Bold),
            ForeColor = Color.White
        };
        Controls.Add(title);

        Label sub = new Label {
            Text = "Fast duplicate & similar photo finder for Windows (64-bit)",
            Location = new Point(24, 52),
            Size = new Size(472, 20),
            Font = new Font("Consolas", 9f, FontStyle.Regular),
            ForeColor = Color.FromArgb(160, 160, 160)
        };
        Controls.Add(sub);

        Label dirLbl = new Label {
            Text = "INSTALL FOLDER:",
            Location = new Point(24, 86),
            Size = new Size(472, 18),
            ForeColor = Color.FromArgb(180, 180, 180)
        };
        Controls.Add(dirLbl);

        string defDir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "Duplikill");
        pathBox = new TextBox {
            Text = defDir,
            Location = new Point(24, 108),
            Size = new Size(472, 24),
            BackColor = Color.Black,
            ForeColor = Color.White,
            BorderStyle = BorderStyle.FixedSingle
        };
        Controls.Add(pathBox);

        cbStartMenu = new CheckBox {
            Text = "Create Start Menu shortcut",
            Checked = true,
            Location = new Point(24, 144),
            Size = new Size(400, 22)
        };
        Controls.Add(cbStartMenu);

        cbDesktop = new CheckBox {
            Text = "Create Desktop shortcut",
            Checked = true,
            Location = new Point(24, 170),
            Size = new Size(400, 22)
        };
        Controls.Add(cbDesktop);

        cbLaunch = new CheckBox {
            Text = "Launch Duplikill when done",
            Checked = true,
            Location = new Point(24, 196),
            Size = new Size(400, 22)
        };
        Controls.Add(cbLaunch);

        bar = new ProgressBar {
            Location = new Point(24, 228),
            Size = new Size(472, 10),
            Style = ProgressBarStyle.Continuous,
            Visible = false
        };
        Controls.Add(bar);

        statusLbl = new Label {
            Text = "READY.",
            Location = new Point(24, 256),
            Size = new Size(280, 28),
            ForeColor = Color.FromArgb(170, 170, 170)
        };
        Controls.Add(statusLbl);

        btnInstall = new Button {
            Text = "INSTALL",
            Location = new Point(336, 248),
            Size = new Size(160, 38),
            BackColor = Color.White,
            ForeColor = Color.Black,
            FlatStyle = FlatStyle.Flat,
            Cursor = Cursors.Hand
        };
        btnInstall.FlatAppearance.BorderSize = 0;
        btnInstall.Click += (s, e) => StartInstall();
        Controls.Add(btnInstall);
    }

    void StartInstall() {
        btnInstall.Enabled = false;
        pathBox.Enabled = false;
        bar.Visible = true;
        statusLbl.Text = "EXTRACTING FILES ...";
        string target = pathBox.Text.Trim();
        bool mkStart = cbStartMenu.Checked;
        bool mkDesk = cbDesktop.Checked;
        bool runApp = cbLaunch.Checked;

        ThreadPool.QueueUserWorkItem(_ => {
            try {
                RunInstall(target, mkStart, mkDesk, p => BeginInvoke((Action)(() => bar.Value = p)));
                BeginInvoke((Action)(() => {
                    statusLbl.Text = "DONE.";
                    if (runApp) {
                        string exe = Path.Combine(target, "Duplikill.exe");
                        if (File.Exists(exe)) Process.Start(new ProcessStartInfo(exe) { WorkingDirectory = target });
                    }
                    Close();
                }));
            } catch (Exception ex) {
                BeginInvoke((Action)(() => {
                    MessageBox.Show(this, ex.Message, "Duplikill Setup Error", MessageBoxButtons.OK, MessageBoxIcon.Error);
                    btnInstall.Enabled = true;
                    pathBox.Enabled = true;
                }));
            }
        });
    }

    public static void RunInstall(string targetDir, bool startMenu, bool desktop, Action<int> progress) {
        string self = Application.ExecutablePath;
        using (FileStream fs = new FileStream(self, FileMode.Open, FileAccess.Read, FileShare.Read)) {
            if (fs.Length < 16) throw new Exception("Invalid installer payload.");
            fs.Seek(-16, SeekOrigin.End);
            byte[] tail = new byte[16];
            fs.Read(tail, 0, 16);
            long magic = BitConverter.ToInt64(tail, 8);
            if (magic != 0x4C4B494C50554421L) throw new Exception("Installer payload signature not found.");
            long zipLen = BitConverter.ToInt64(tail, 0);
            long zipStart = fs.Length - 16 - zipLen;
            fs.Seek(zipStart, SeekOrigin.Begin);

            if (Directory.Exists(targetDir)) {
                try { Directory.Delete(targetDir, true); } catch {}
            }
            Directory.CreateDirectory(targetDir);

            using (SubStream ss = new SubStream(fs, zipStart, zipLen))
            using (ZipArchive za = new ZipArchive(ss, ZipArchiveMode.Read)) {
                int total = Math.Max(1, za.Entries.Count);
                int done = 0;
                foreach (ZipArchiveEntry entry in za.Entries) {
                    done++;
                    string rel = entry.FullName;
                    if (rel.StartsWith("Duplikill/") || rel.StartsWith("Duplikill\\"))
                        rel = rel.Substring(10);
                    if (string.IsNullOrEmpty(rel)) continue;
                    string outPath = Path.Combine(targetDir, rel.Replace('/', Path.DirectorySeparatorChar));
                    if (entry.FullName.EndsWith("/") || entry.FullName.EndsWith("\\")) {
                        Directory.CreateDirectory(outPath);
                        continue;
                    }
                    Directory.CreateDirectory(Path.GetDirectoryName(outPath));
                    entry.ExtractToFile(outPath, true);
                    if (progress != null && done % 15 == 0)
                        progress(Math.Min(100, (done * 100) / total));
                }
            }
        }
        if (progress != null) progress(100);
        string exePath = Path.Combine(targetDir, "Duplikill.exe");
        if (startMenu) {
            string sm = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), "Duplikill.lnk");
            MakeShortcut(sm, exePath, targetDir);
        }
        if (desktop) {
            string dk = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), "Duplikill.lnk");
            MakeShortcut(dk, exePath, targetDir);
        }
        RegisterUninstaller(targetDir, exePath);
    }

    static void MakeShortcut(string lnkPath, string targetExe, string workDir) {
        try {
            Type t = Type.GetTypeFromProgID("WScript.Shell");
            object shell = Activator.CreateInstance(t);
            object sc = t.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { lnkPath });
            Type st = sc.GetType();
            st.InvokeMember("TargetPath", BindingFlags.SetProperty, null, sc, new object[] { targetExe });
            st.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, sc, new object[] { workDir });
            st.InvokeMember("IconLocation", BindingFlags.SetProperty, null, sc, new object[] { targetExe + ",0" });
            st.InvokeMember("Save", BindingFlags.InvokeMethod, null, sc, null);
        } catch {}
    }

    static void RegisterUninstaller(string targetDir, string exePath) {
        try {
            string unbat = Path.Combine(targetDir, "Uninstall.cmd");
            File.WriteAllText(unbat,
                "@echo off\r\n" +
                "del /f /q \"%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Duplikill.lnk\" 2>nul\r\n" +
                "del /f /q \"%USERPROFILE%\\Desktop\\Duplikill.lnk\" 2>nul\r\n" +
                "reg delete \"HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\Duplikill\" /f 2>nul\r\n" +
                "cd /d \"%TEMP%\"\r\n" +
                "start /b \"\" cmd /c \"ping 127.0.0.1 -n 2 >nul & rmdir /s /q \"" + targetDir + "\"\"\r\n");
            using (RegistryKey k = Registry.CurrentUser.CreateSubKey(@"Software\Microsoft\Windows\CurrentVersion\Uninstall\Duplikill")) {
                k.SetValue("DisplayName", "Duplikill");
                k.SetValue("DisplayVersion", "0.1.0");
                k.SetValue("Publisher", "metamountain");
                k.SetValue("DisplayIcon", exePath);
                k.SetValue("InstallLocation", targetDir);
                k.SetValue("UninstallString", "\"" + unbat + "\"");
                k.SetValue("NoModify", 1, RegistryValueKind.DWord);
                k.SetValue("NoRepair", 1, RegistryValueKind.DWord);
            }
        } catch {}
    }

    [STAThread]
    static int Main(string[] args) {
        if (args.Length > 0 && args[0] == "--silent") {
            string defDir = args.Length > 1 ? args[1] :
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "Duplikill");
            RunInstall(defDir, false, false, null);
            return 0;
        }
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new SetupForm());
        return 0;
    }
}

class SubStream : Stream {
    readonly Stream baseStream;
    readonly long start;
    readonly long length;
    long pos;
    public SubStream(Stream s, long start, long length) {
        baseStream = s; this.start = start; this.length = length; pos = 0;
    }
    public override bool CanRead { get { return true; } }
    public override bool CanSeek { get { return true; } }
    public override bool CanWrite { get { return false; } }
    public override long Length { get { return length; } }
    public override long Position {
        get { return pos; }
        set { pos = value; baseStream.Seek(start + pos, SeekOrigin.Begin); }
    }
    public override int Read(byte[] buffer, int offset, int count) {
        long rem = length - pos;
        if (rem <= 0) return 0;
        if (count > rem) count = (int)rem;
        baseStream.Seek(start + pos, SeekOrigin.Begin);
        int n = baseStream.Read(buffer, offset, count);
        pos += n;
        return n;
    }
    public override long Seek(long offset, SeekOrigin origin) {
        if (origin == SeekOrigin.Begin) Position = offset;
        else if (origin == SeekOrigin.Current) Position = pos + offset;
        else if (origin == SeekOrigin.End) Position = length + offset;
        return pos;
    }
    public override void Flush() {}
    public override void SetLength(long value) { throw new NotSupportedException(); }
    public override void Write(byte[] buffer, int offset, int count) { throw new NotSupportedException(); }
}
"""


def ensure_czkawka():
    exe = os.path.join(ROOT, "tools", "windows_czkawka_cli.exe")
    if not os.path.exists(exe):
        print(f"Downloading windows_czkawka_cli.exe from {CZKAWKA_URL} ...")
        os.makedirs(os.path.dirname(exe), exist_ok=True)
        urllib.request.urlretrieve(CZKAWKA_URL, exe)


def build():
    ensure_czkawka()
    a = lambda *p: os.path.join(ROOT, *p)
    cmd = [PYI, "--noconfirm", "--onedir", "--windowed", "--name", "Duplikill",
           "--icon", a("assets", "duplikill.ico"),
           "--add-data", a("tools", "windows_czkawka_cli.exe") + ";tools",
           "--add-data", a("tools", "fonts") + ";tools/fonts",
           "--add-data", a("assets", "duplikill.png") + ";assets",
           "--workpath", a("tmp", "build"), "--specpath", a("tmp", "build"), "--distpath", a("dist")]
    for m in ("pytest", "_pytest", "pygments", "torch", "tkinter", "PyInstaller", "PySide6.QtNetwork",
              "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtPdf", "PySide6.QtOpenGL"):
        cmd += ["--exclude-module", m]
    cmd.append(a("run.py"))
    subprocess.run(cmd, check=True, cwd=ROOT)


def strip():
    base = os.path.join(DIST, "_internal", "PySide6")
    for rel in UNUSED:
        for p in glob.glob(os.path.join(base, rel)):
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)


def build_zip():
    if os.path.exists(ZIP_OUT):
        os.remove(ZIP_OUT)
    with zipfile.ZipFile(ZIP_OUT, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for dp, _, fs in os.walk(DIST):
            for f in fs:
                full = os.path.join(dp, f)
                rel = os.path.relpath(full, os.path.dirname(DIST))
                zf.write(full, rel)


def build_installer():
    if not os.path.exists(CSC):
        print("Skipping Setup.exe: .NET csc.exe not found")
        return
    build_dir = os.path.join(ROOT, "tmp", "build")
    os.makedirs(build_dir, exist_ok=True)
    cs_path = os.path.join(build_dir, "Setup.cs")
    stub_exe = os.path.join(build_dir, "SetupStub.exe")
    with open(cs_path, "w", encoding="utf-8") as fh:
        fh.write(INSTALLER_CS)
    ico = os.path.join(ROOT, "assets", "duplikill.ico")
    cmd = [
        CSC, "/nologo", "/target:winexe", "/optimize+",
        f"/out:{stub_exe}", f"/win32icon:{ico}",
        "/r:System.dll", "/r:System.Drawing.dll", "/r:System.Windows.Forms.dll",
        "/r:System.IO.Compression.dll", "/r:System.IO.Compression.FileSystem.dll",
        cs_path,
    ]
    subprocess.run(cmd, check=True, cwd=ROOT)
    zip_size = os.path.getsize(ZIP_OUT)
    with open(SETUP_OUT, "wb") as out_f:
        with open(stub_exe, "rb") as stub_f:
            shutil.copyfileobj(stub_f, out_f)
        with open(ZIP_OUT, "rb") as zip_f:
            shutil.copyfileobj(zip_f, out_f)
        out_f.write(zip_size.to_bytes(8, "little"))
        out_f.write((0x4C4B494C50554421).to_bytes(8, "little"))


def size_mb(path) -> float:
    if os.path.isfile(path):
        return os.path.getsize(path) / 2 ** 20
    return sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(path) for f in fs) / 2 ** 20


def smoke_test() -> bool:
    """Starts the exe for a few seconds: it must still be running (no crash on import)."""
    p = subprocess.Popen([os.path.join(DIST, "Duplikill.exe")])
    time.sleep(6)
    alive = p.poll() is None
    if alive:
        p.kill()
    return alive


if __name__ == "__main__":
    if "--package-only" not in sys.argv:
        build()
        strip()
    print(f"Folder size: {size_mb(DIST):.0f} MB")
    ok = smoke_test()
    print("Smoke test:", "OK" if ok else "CRASHED")
    if not ok:
        sys.exit(1)
    build_zip()
    print(f"Portable ZIP: {ZIP_OUT} ({size_mb(ZIP_OUT):.1f} MB)")
    build_installer()
    print(f"Setup EXE:    {SETUP_OUT} ({size_mb(SETUP_OUT):.1f} MB)")
