using Amane;

var root = Path.Combine(Path.GetTempPath(), "Amane.Source.Checks", Guid.NewGuid().ToString("N"));
Directory.CreateDirectory(root);
var app = Path.Combine(root, "app");
var source = Path.Combine(root, "source with spaces");
Directory.CreateDirectory(app);
Directory.CreateDirectory(source);
var sourceFile = Path.Combine(app, "source-root.txt");
var required = new[] { "pyproject.toml", "src/amane/server.py", ".venv/Scripts/python.exe", "web/dist/index.html" };
foreach (var file in required)
{
    var path = Path.Combine(source, file);
    Directory.CreateDirectory(Path.GetDirectoryName(path)!);
    File.WriteAllText(path, "validation fixture");
}

static void Check(bool condition, string message)
{
    if (!condition) throw new Exception(message);
}

Check(ServerLaunch.Resolve(app, null) is null, "Missing package must not select a server");
foreach (var configured in new[] { "../source with spaces", source })
{
    File.WriteAllText(sourceFile, configured + "\r\n");
    var launch = ServerLaunch.Resolve(app, null)!;
    Check(launch.SourceRoot == source, "Source root resolution failed");
    Check(launch.WorkingDirectory == source, "Source working directory failed");
    Check(launch.Executable == Path.Combine(source, ".venv", "Scripts", "python.exe"), "Interpreter selection failed");
}
foreach (var missing in required)
{
    var path = Path.Combine(source, missing);
    File.Delete(path);
    try
    {
        ServerLaunch.Resolve(app, null);
        throw new Exception("Missing source dependency was accepted: " + missing);
    }
    catch (FileNotFoundException ex)
    {
        Check(ex.FileName == path && ex.Message.Contains(missing), "Missing dependency diagnostic is unclear");
    }
    File.WriteAllText(path, "validation fixture");
}
File.WriteAllText(sourceFile, " \r\n");
try
{
    ServerLaunch.Resolve(app, null);
    throw new Exception("Empty source root was accepted");
}
catch (InvalidOperationException ex)
{
    Check(ex.Message.Contains("source-root.txt"), "Empty source root diagnostic is unclear");
}
var overrideFile = Path.Combine(root, "override.exe");
File.WriteAllText(overrideFile, "validation fixture");
Check(ServerLaunch.Resolve(app, overrideFile)!.Executable == overrideFile, "Binary override must take priority");
Check(ServerLaunch.Resolve(app, overrideFile + ".missing") is null, "Missing binary override was accepted");
File.Delete(sourceFile);
var packaged = Path.Combine(app, "onedir", "Amane.Server.exe");
Directory.CreateDirectory(Path.GetDirectoryName(packaged)!);
File.WriteAllText(packaged, "validation fixture");
Check(ServerLaunch.Resolve(app, null)!.Executable == packaged, "Packaged mode regression");

var parsed = DesktopSettings.Parse("AMANE_PORT='18975'\nAMANE_PORT=18976\nAMANE_DATA_DIR=\"relative data\"\nAMANE_BIN=bad.exe\ninvalid\n");
Check(parsed.Values["AMANE_PORT"] == "18976", "Later settings must override earlier settings");
Check(parsed.Values["AMANE_DATA_DIR"] == "relative data", "Quoted paths were not preserved");
Check(!parsed.Values.ContainsKey("AMANE_BIN") && parsed.UnknownKeys.SequenceEqual(new[] { "AMANE_BIN", "invalid" }), "Invalid settings were not rejected");
var settings = Path.Combine(app, "desktop.env");
DesktopSettings.CreateIfMissing(settings, source, "8000");
var template = File.ReadAllText(settings);
Check(template.Contains(source) && template.Contains("AMANE_PORT=8000"), "Source template defaults are incorrect");
File.WriteAllText(settings, "AMANE_PORT=18977");
DesktopSettings.CreateIfMissing(settings, source, "8000");
Check(File.ReadAllText(settings) == "AMANE_PORT=18977", "Existing settings were overwritten");
Console.WriteLine("Source launch path, dependency errors, packaged mode, settings validation and preservation: passed");

