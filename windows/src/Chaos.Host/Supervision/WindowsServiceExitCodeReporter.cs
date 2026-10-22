using System.Runtime.Versioning;
using Chaos.Host.Supervisor;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;

namespace Chaos.Host.Supervision;

/// <summary>
/// Reports a self-initiated exit code to everyone who can see it: the
/// process (<see cref="Environment.ExitCode"/>, which <c>Main</c> returns)
/// and, when running under the Windows service lifetime, the service control
/// manager through the service's own exit code.
/// </summary>
/// <remarks>
/// SCM records a service that stops with a non-zero service-specific code as
/// "stopped with an error". Whether it then runs the recovery policy depends
/// on the service's failure flag — see <see cref="SupervisionFailureHostStop"/>.
/// </remarks>
internal sealed class WindowsServiceExitCodeReporter : IHostExitCodeReporter
{
    private readonly IServiceProvider _services;

    public WindowsServiceExitCodeReporter(IServiceProvider services) => _services = services;

    public void Report(int exitCode)
    {
        Environment.ExitCode = exitCode;

        if (OperatingSystem.IsWindows())
        {
            ReportToServiceControlManager(exitCode);
        }
    }

    [SupportedOSPlatform("windows")]
    private void ReportToServiceControlManager(int exitCode)
    {
        // The lifetime is resolved lazily and only here: the registration is
        // conditional on being started by SCM, and resolving it at construction
        // would pull the whole service lifetime into a console session.
        if (_services.GetService<IHostLifetime>() is System.ServiceProcess.ServiceBase service)
        {
            service.ExitCode = exitCode;
        }
    }
}
