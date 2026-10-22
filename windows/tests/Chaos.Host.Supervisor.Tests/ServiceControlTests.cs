using Chaos.Host.Supervisor.ServiceControl;
using Xunit;

namespace Chaos.Host.Supervisor.Tests;

/// <summary>
/// The host's service-control switches and the registration plan behind them.
/// </summary>
/// <remarks>
/// These are the hand path (<c>--install-service</c> at an elevated prompt)
/// and the installer's <c>ChaosServiceRegistration=host</c> path, both of
/// which call the same code. A wrong plan here is a service that does not
/// start on a machine nobody is sitting at.
/// </remarks>
public sealed class ServiceControlTests
{
    [Fact]
    public void No_arguments_means_run_as_the_service_or_a_plain_process()
    {
        var parsed = ServiceCommandLine.Parse([]);

        Assert.Equal(HostRunMode.RunService, parsed.Mode);
        Assert.Empty(parsed.Remaining);
        Assert.Null(parsed.Error);
    }

    [Theory]
    [InlineData("--install-service", HostRunMode.InstallService)]
    [InlineData("--uninstall-service", HostRunMode.UninstallService)]
    [InlineData("--console", HostRunMode.Console)]
    [InlineData("--service", HostRunMode.RunService)]
    [InlineData("--help", HostRunMode.Help)]
    [InlineData("-h", HostRunMode.Help)]
    [InlineData("/?", HostRunMode.Help)]
    public void Each_switch_selects_its_mode_and_is_consumed(string flag, HostRunMode expected)
    {
        var parsed = ServiceCommandLine.Parse([flag]);

        Assert.Equal(expected, parsed.Mode);
        Assert.Empty(parsed.Remaining);
        Assert.Null(parsed.Error);
    }

    [Fact]
    public void Everything_else_is_left_for_the_host_in_order()
    {
        var parsed = ServiceCommandLine.Parse(["--Chaos:ListenUrl=http://0.0.0.0:9090", "--console", "--urls", "http://127.0.0.1:0"]);

        Assert.Equal(HostRunMode.Console, parsed.Mode);
        Assert.Equal(["--Chaos:ListenUrl=http://0.0.0.0:9090", "--urls", "http://127.0.0.1:0"], parsed.Remaining);
    }

    [Fact]
    public void Contradictory_switches_are_refused_with_the_names_in_the_message()
    {
        var parsed = ServiceCommandLine.Parse(["--install-service", "--console"]);

        Assert.Equal(HostRunMode.Help, parsed.Mode);
        Assert.NotNull(parsed.Error);
        Assert.Contains("--install-service", parsed.Error, StringComparison.Ordinal);
        Assert.Contains("--console", parsed.Error, StringComparison.Ordinal);
    }

    [Fact]
    public void The_same_switch_twice_is_not_a_contradiction()
    {
        Assert.Equal(HostRunMode.Console, ServiceCommandLine.Parse(["--console", "--console"]).Mode);
    }

    [Fact]
    public void Usage_names_every_switch()
    {
        var usage = ServiceCommandLine.Usage;

        foreach (var flag in new[] { "--install-service", "--uninstall-service", "--console", "--service", "--help" })
        {
            Assert.Contains(flag, usage, StringComparison.Ordinal);
        }
    }

    [Fact]
    public void The_install_plan_registers_a_virtual_account_recovery_the_failure_flag_and_the_event_log_source()
    {
        var options = new ServiceInstallOptions { BinaryPath = @"C:\Program Files\Project CHAOS\Chaos.Host.exe" };

        var plan = ServiceRegistrationPlan.Install(options);

        var create = plan[0];
        Assert.Equal("sc.exe", create.FileName);
        Assert.Equal("create", create.Arguments[0]);
        Assert.Equal("ChaosHost", create.Arguments[1]);
        Assert.Contains(@"""C:\Program Files\Project CHAOS\Chaos.Host.exe"" --service", create.Arguments);
        Assert.Contains(@"NT SERVICE\ChaosHost", create.Arguments);
        Assert.Contains("Tcpip", create.Arguments);
        Assert.Contains("auto", create.Arguments);

        Assert.Contains(plan, command => command.Arguments[0] == "description");
        Assert.Contains(plan, command => command.Arguments[0] == "failure" && command.Arguments.Any(a => a.StartsWith("restart/5000/", StringComparison.Ordinal)));

        // The breaker exit is a stop with an error, not a crash; without this
        // flag SCM would not count it and the recovery policy would never run.
        Assert.Contains(plan, command => command.Arguments[0] == "failureflag" && command.Arguments[2] == "1");

        var eventLog = plan[^1];
        Assert.Equal("powershell.exe", eventLog.FileName);
        Assert.Contains(eventLog.Arguments, a => a.Contains("New-EventLog", StringComparison.Ordinal) && a.Contains("'Project CHAOS'", StringComparison.Ordinal));
    }

    [Fact]
    public void The_failure_flag_can_be_left_out()
    {
        var plan = ServiceRegistrationPlan.Install(new ServiceInstallOptions
        {
            BinaryPath = @"C:\x\Chaos.Host.exe",
            RecoverOnNonCrashFailure = false,
        });

        Assert.DoesNotContain(plan, command => command.Arguments[0] == "failureflag");
    }

    [Fact]
    public void The_uninstall_plan_stops_deletes_and_removes_the_source()
    {
        var plan = ServiceRegistrationPlan.Uninstall(new ServiceInstallOptions());

        Assert.Equal(["stop", "delete"], plan.Take(2).Select(command => command.Arguments[0]));
        Assert.All(plan.Take(2), command => Assert.Equal("ChaosHost", command.Arguments[1]));
        Assert.Contains(plan[^1].Arguments, a => a.Contains("Remove-EventLog", StringComparison.Ordinal));
    }

    [Fact]
    public void A_published_executable_registers_itself()
    {
        var options = ServiceInstallOptions.ForProcess(@"C:\Program Files\Project CHAOS\Chaos.Host.exe", @"C:\Program Files\Project CHAOS\Chaos.Host.dll");

        Assert.Equal(@"C:\Program Files\Project CHAOS\Chaos.Host.exe", options.BinaryPath);
        Assert.Equal("--service", options.ServiceArgument);
    }

    [Fact]
    public void A_dotnet_hosted_process_registers_the_muxer_with_the_assembly()
    {
        var options = ServiceInstallOptions.ForProcess(@"C:\Program Files\dotnet\dotnet.exe", @"C:\src\Chaos.Host.dll");

        Assert.Equal(@"C:\Program Files\dotnet\dotnet.exe", options.BinaryPath);
        Assert.Equal(@"""C:\src\Chaos.Host.dll"" --service", options.ServiceArgument);

        var create = ServiceRegistrationPlan.Install(options)[0];
        Assert.Contains(@"""C:\Program Files\dotnet\dotnet.exe"" ""C:\src\Chaos.Host.dll"" --service", create.Arguments);
    }

    [Fact]
    public void Describe_is_a_script_an_operator_can_read()
    {
        var text = ServiceRegistrationPlan.Describe(ServiceRegistrationPlan.Uninstall(new ServiceInstallOptions()));

        Assert.Contains(":: stop the service", text, StringComparison.Ordinal);
        Assert.Contains("sc.exe stop ChaosHost", text, StringComparison.Ordinal);
        Assert.Contains("sc.exe delete ChaosHost", text, StringComparison.Ordinal);
    }

    [Fact]
    public void Off_windows_the_installer_refuses_and_prints_the_plan_instead()
    {
        if (OperatingSystem.IsWindows())
        {
            return;
        }

        var output = new StringWriter();
        var code = ServiceInstaller.Execute(
            ServiceRegistrationPlan.Install(new ServiceInstallOptions { BinaryPath = "/opt/chaos/Chaos.Host" }),
            output);

        Assert.Equal(ServiceInstaller.Unsupported, code);
        Assert.Contains("not possible on this operating system", output.ToString(), StringComparison.Ordinal);
        Assert.Contains("sc.exe create ChaosHost", output.ToString(), StringComparison.Ordinal);
    }
}
