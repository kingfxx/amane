namespace Amane;

internal sealed record ServerLaunch(string Executable, string WorkingDirectory, string? SourceRoot = null)
{
    internal static ServerLaunch? Resolve(string appDirectory, string? overrideBinary)
    {
        if (!string.IsNullOrEmpty(overrideBinary))
        {
            var binary = Path.GetFullPath(overrideBinary);
            return File.Exists(binary) ? new(binary, Path.GetDirectoryName(binary)!) : null;
        }

        var sourceFile = Path.Combine(appDirectory, "source-root.txt");
        if (File.Exists(sourceFile))
        {
            var configured = File.ReadAllText(sourceFile).Trim();
            if (configured.Length == 0)
            {
                throw new InvalidOperationException("source-root.txt 未指定源码目录。 / Source directory is missing.");
            }

            var root = Path.GetFullPath(configured, appDirectory);
            foreach (var relative in new[]
            {
                "pyproject.toml", "src/amane/server.py", ".venv/Scripts/python.exe", "web/dist/index.html",
            })
            {
                if (!File.Exists(Path.Combine(root, relative)))
                {
                    throw new FileNotFoundException(
                        $"源码启动所需文件不存在，请执行 just sync 和 just build。 / Required source file is missing: {relative}",
                        Path.Combine(root, relative)
                    );
                }
            }

            return new(Path.Combine(root, ".venv", "Scripts", "python.exe"), root, root);
        }

        foreach (var name in new[] { "Amane.Server.exe", "Amane.exe" })
        {
            var binary = Path.Combine(appDirectory, "onedir", name);
            if (File.Exists(binary))
            {
                return new(binary, Path.GetDirectoryName(binary)!);
            }
        }

        return null;
    }
}
