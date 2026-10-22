using System.Diagnostics;
using System.Globalization;

namespace Chaos.Host.Supervisor.ServiceControl;

/// <summary>How the host was asked to run.</summary>
public enum HostRunMode
{
    /// <summary>No mode switch: run as a Windows Service (or a plain process).</summary>
    RunService = 0,

    /// <summary>Run in the foreground with a console, for commissioning and debugging.</summary>
    Console = 1,

    /// <summary>Register the Windows Service and exit.</summary>
    InstallService = 2,

    /// <summary>Remove the Windows Service and exit.</summary>
    UninstallService = 3,

    /// <summary>Print usage and exit.</summary>
    Help = 4,
}

/// <summary>Result of parsing the host's command line.</summary>
/// <param name="Mode">What was asked for.</param>
/// <param name="Remaining">Arguments not consumed here, for the host to parse.</param>
/// <param name="Error">Non-null when the command line was contradictory.</param>
public sealed record HostCommandLine(HostRunMode Mode, IReadOnlyList<string> Remaining, string? Error = null);

/// <summary>
/// The service-control surface of the host's command line.
/// </summary>
/// <remarks>
/// The gateway owns <c>Program.Main</c> (<c>Chaos.Host/Program.cs</c>) and
/// parses with this first, before the web host sees the arguments; this is
/// the piece that belongs with the supervisor, so the flag names, the
/// registration plan and the installer's custom actions all agree. The
/// operator-facing description is docs/advanced-command-line.md, "The
/// gateway's own switches".
/// </remarks>
public static class ServiceCommandLine
{
    public const string InstallFlag = "--install-service";
    public const string UninstallFlag = "--uninstall-service";
    public const string ConsoleFlag = "--console";

    /// <summary>
    /// What the registered service's command line carries
    /// (<see cref="ServiceInstallOptions.ServiceArgument"/>). It means "run
    /// as the service" and is consumed here so it never reaches the web
    /// host's own command-line configuration.
    /// </summary>
    public const string ServiceFlag = "--service";

    public static HostCommandLine Parse(IReadOnlyList<string> arguments)
    {
        ArgumentNullException.ThrowIfNull(arguments);

        var modes = new List<HostRunMode>();
        var remaining = new List<string>();

        foreach (var argument in arguments)
        {
            switch (argument)
            {
                case InstallFlag:
                    modes.Add(HostRunMode.InstallService);
                    break;
                case UninstallFlag:
                    modes.Add(HostRunMode.UninstallService);
                    break;
                case ConsoleFlag:
                    modes.Add(HostRunMode.Console);
                    break;
                case ServiceFlag:
                    modes.Add(HostRunMode.RunService);
                    break;
                case "--help":
                case "-h":
                case "-?":
                case "/?":
                    modes.Add(HostRunMode.Help);
                    break;
                default:
                    remaining.Add(argument);
                    break;
            }
        }

        var distinct = modes.Distinct().ToList();
        if (distinct.Count > 1)
        {
            return new HostCommandLine(
                HostRunMode.Help,
                remaining,
                $"These options cannot be combined: {string.Join(", ", distinct.Select(Flag))}.");
        }

        return new HostCommandLine(distinct.Count == 1 ? distinct[0] : HostRunMode.RunService, remaining);
    }

    /// <summary>Usage text for the service-control flags.</summary>
    public static string Usage =>
        $"""
         Project CHAOS host (Chaos.Host)

         Service control:
           {InstallFlag}     Register the Windows Service (requires an elevated prompt), then exit.
           {UninstallFlag}   Stop and remove the Windows Service, then exit.
           {ConsoleFlag}             Run in the foreground with a console instead of as a service.
           {ServiceFlag}             Run as the Windows Service; what the registered service passes.
           --help                Print this and exit.

         With no option the host runs as a Windows Service when started by the Service Control
         Manager, and as an ordinary foreground process otherwise. Every other argument is
         passed to the host's configuration (for example --Chaos:ListenUrl=http://0.0.0.0:8080).

         Exit codes for {InstallFlag} / {UninstallFlag}: 0 done, 1 a command failed
         (usually: not elevated), 3 not possible on this operating system.
         """;

    private static string Flag(HostRunMode mode) => mode switch
    {
        HostRunMode.InstallService => InstallFlag,
        HostRunMode.UninstallService => UninstallFlag,
        HostRunMode.Console => ConsoleFlag,
        HostRunMode.RunService => ServiceFlag,
        _ => "--help",
    };
}

/// <summary>Runs a registration plan.</summary>
public static class ServiceInstaller
{
    /// <summary>Exit code meaning the plan ran to completion.</summary>
    public const int Success = 0;

    /// <summary>Exit code meaning the plan could not run at all.</summary>
    public const int Unsupported = 3;

    /// <summary>Exit code meaning a command in the plan failed.</summary>
    public const int CommandFailed = 1;

    /// <summary>
    /// Executes a plan, writing each command and its result to
    /// <paramref name="output"/>. Off Windows it refuses and says why, rather
    /// than pretending to have installed anything.
    /// </summary>
    /// <param name="plan">Commands from <see cref="ServiceRegistrationPlan"/>.</param>
    /// <param name="output">Where progress goes; typically <c>Console.Out</c>.</param>
    /// <param name="continueOnFailure">
    /// True for uninstall, where "the service was not there" is not an error.
    /// </param>
    public static int Execute(IReadOnlyList<ServiceCommand> plan, TextWriter output, bool continueOnFailure = false)
    {
        ArgumentNullException.ThrowIfNull(plan);
        ArgumentNullException.ThrowIfNull(output);

        if (!OperatingSystem.IsWindows())
        {
            output.WriteLine(
                "Windows Service registration is not possible on this operating system. " +
                "The plan that would have run:");
            output.WriteLine(ServiceRegistrationPlan.Describe(plan));
            return Unsupported;
        }

        var failed = 0;

        foreach (var command in plan)
        {
            output.WriteLine($":: {command.Purpose}");
            output.WriteLine(command.ToCommandLine());

            var exitCode = Run(command, output);
            if (exitCode == Success)
            {
                continue;
            }

            failed++;
            output.WriteLine(
                $"   -> exit code {exitCode.ToString(CultureInfo.InvariantCulture)}" +
                (continueOnFailure ? " (continuing)" : string.Empty));

            if (!continueOnFailure)
            {
                output.WriteLine(
                    "Registration stopped. The usual cause is running without administrator rights: " +
                    "sc.exe needs an elevated prompt.");
                return CommandFailed;
            }
        }

        return failed == 0 || continueOnFailure ? Success : CommandFailed;
    }

    private static int Run(ServiceCommand command, TextWriter output)
    {
        var startInfo = new ProcessStartInfo
        {
            FileName = command.FileName,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
        };

        foreach (var argument in command.Arguments)
        {
            startInfo.ArgumentList.Add(argument);
        }

        try
        {
            using var process = Process.Start(startInfo);
            if (process is null)
            {
                output.WriteLine($"   -> could not start {command.FileName}");
                return CommandFailed;
            }

            var standardOutput = process.StandardOutput.ReadToEnd();
            var standardError = process.StandardError.ReadToEnd();
            process.WaitForExit();

            foreach (var line in Lines(standardOutput).Concat(Lines(standardError)))
            {
                output.WriteLine("   " + line);
            }

            return process.ExitCode;
        }
        catch (Exception ex) when (ex is System.ComponentModel.Win32Exception or InvalidOperationException or IOException)
        {
            output.WriteLine($"   -> {ex.GetType().Name}: {ex.Message}");
            return CommandFailed;
        }
    }

    private static IEnumerable<string> Lines(string text) =>
        text.Split('\n', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
}
