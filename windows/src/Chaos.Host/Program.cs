using Chaos.Host;
using Chaos.Host.Supervisor.ServiceControl;

// -----------------------------------------------------------------------------
// Project CHAOS - .NET gateway host.
//
// The front door for the platform: the only listener on the LAN, a reverse
// proxy to the Python backend on loopback, the host of the operator console,
// and the place ported subsystems land as the migration proceeds.
//
// Everything is assembled in ChaosHostExtensions so the same host can be built
// from a test or from another entry point without the registration order
// drifting - and the order matters here: the route-ownership manifest is
// validated before anything else starts.
//
// The service-control switches are parsed FIRST, before the web host sees the
// arguments: --install-service and --uninstall-service run a registration plan
// and exit, --console runs the host in the foreground without the Windows
// Service lifetime, and --service (what the registered service passes) is
// consumed so it never reaches the configuration binder. Everything else goes
// to the host as usual.
// -----------------------------------------------------------------------------

var commandLine = ServiceCommandLine.Parse(args);

switch (commandLine.Mode)
{
    case HostRunMode.Help:
        if (commandLine.Error is not null)
        {
            Console.Error.WriteLine(commandLine.Error);
            Console.Error.WriteLine();
        }

        Console.WriteLine(ServiceCommandLine.Usage);
        return commandLine.Error is null ? 0 : 2;

    case HostRunMode.InstallService:
        return ServiceInstaller.Execute(
            ServiceRegistrationPlan.Install(ServiceInstallOptions.ForProcess(
                Environment.ProcessPath,
                typeof(Program).Assembly.Location)),
            Console.Out);

    case HostRunMode.UninstallService:
        return ServiceInstaller.Execute(
            ServiceRegistrationPlan.Uninstall(ServiceInstallOptions.ForProcess(
                Environment.ProcessPath,
                typeof(Program).Assembly.Location)),
            Console.Out,
            continueOnFailure: true);
}

var builder = WebApplication.CreateBuilder([.. commandLine.Remaining]);

builder.AddChaosHost(windowsServiceLifetime: commandLine.Mode != HostRunMode.Console);

var app = builder.Build();

app.UseChaosHost();

app.Run();

// A host that stopped itself - the backend supervisor's circuit breaker does
// this - has set Environment.ExitCode; returning it is what makes the exit
// code visible to the service control manager and to a shell.
return Environment.ExitCode;

/// <summary>
/// Entry point. Declared partial and public so
/// <c>WebApplicationFactory&lt;Program&gt;</c> can host the real pipeline in tests
/// rather than a reconstruction of it.
/// </summary>
public partial class Program;
