using System.Text.Json;
using Chaos.Host.Abstractions;
using Chaos.Host.Supervisor;
using Chaos.Host.Tests.Support;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;

namespace Chaos.Host.Tests;

/// <summary>
/// The gateway really registers, configures and drives the backend supervisor
/// when <c>Chaos:SuperviseBackend</c> is on.
/// </summary>
/// <remarks>
/// This is the regression the shared abstractions project exists for. Before
/// it, the supervisor registered an <c>IBackendSupervisor</c> of its own that
/// merely shared a name with the gateway's, the gateway's no-op fallback won
/// the container, and <c>/host/info</c> reported <c>registered: false</c>
/// with supervision switched on while nothing started the backend.
/// </remarks>
public sealed class SupervisorWiringTests
{
    [Fact]
    public async Task Host_info_reports_the_real_supervisor_when_supervision_is_on()
    {
        var backend = new FakeSupervisedBackend();
        using var factory = ChaosHostFactory.For()
            .WithSupervisedBackend(backend)
            .With("BackendStartTimeout", "00:00:05")
            .Build();
        using var client = factory.CreateClient();

        var supervisor = factory.Services.GetRequiredService<IBackendSupervisor>();
        Assert.IsType<BackendSupervisor>(supervisor);

        // Readiness is gated on the (fake) /health answering, which it does
        // immediately; the hosted service still runs the start asynchronously
        // relative to the first request, so wait for the contract state.
        await TestExtensions.WaitUntilAsync(
            async () => (await (await client.GetAsync("/host/info")).ReadJsonAsync())
                .GetProperty("supervisor").GetStringProperty("state") == "Running",
            "the supervisor to report the backend Running");

        var body = await (await client.GetAsync("/host/info")).ReadJsonAsync();
        var reported = body.GetProperty("supervisor");

        Assert.True(reported.GetProperty("registered").GetBoolean());
        Assert.Equal(typeof(BackendSupervisor).FullName, reported.GetStringProperty("implementation"));
        Assert.Equal("Running", reported.GetStringProperty("state"));
        Assert.Equal("Running", reported.GetStringProperty("supervisionState"));
        Assert.Equal(0, reported.GetProperty("restartCount").GetInt32());

        // The richer diagnostics ride along: which Python, and where health is polled.
        Assert.Equal("Development", reported.GetProperty("runtime").GetStringProperty("layout"));
        Assert.Contains("FAKE development layout", reported.GetProperty("runtime").GetStringProperty("description"), StringComparison.Ordinal);
        Assert.Contains("/health", reported.GetStringProperty("healthEndpoint"), StringComparison.Ordinal);

        // And it really launched something: the platform CLI's serve verb,
        // on the gateway's backend address.
        var launch = Assert.Single(backend.Processes);
        Assert.Contains("serve", launch.Spec.Arguments);
        Assert.Contains("chaos.cli", launch.Spec.Arguments);
        Assert.Equal(launch.Id, reported.GetProperty("processId").GetInt32());
    }

    [Fact]
    public async Task Health_carries_the_supervisor_too()
    {
        var backend = new FakeSupervisedBackend();
        using var factory = ChaosHostFactory.For()
            .WithSupervisedBackend(backend)
            .Build();
        using var client = factory.CreateClient();

        await TestExtensions.WaitUntilAsync(
            async () => (await (await client.GetAsync("/health")).ReadJsonAsync())
                .GetProperty("supervisor").GetStringProperty("state") == "Running",
            "the supervisor to report the backend Running on /health");

        var body = await (await client.GetAsync("/health")).ReadJsonAsync();
        Assert.True(body.GetProperty("supervisor").GetProperty("registered").GetBoolean());

        // The supervisor saying Running is NOT the gateway saying up: the
        // gateway's own poll of the (absent) backend decides that, and here
        // nothing is listening on the backend address.
        Assert.NotEqual("up", body.GetStringProperty("backend"));
    }

    [Fact]
    public void The_gateway_owns_the_supervisors_address_lifecycle_and_start_budget()
    {
        var backend = new FakeSupervisedBackend();
        using var factory = ChaosHostFactory.For()
            .WithSupervisedBackend(backend)
            .With("BackendUrl", "http://127.0.0.1:18333")
            .With("BackendStartTimeout", "00:00:07")
            .Build();

        // Resolving forces the host to build without sending a request.
        var options = factory.Services.GetRequiredService<IOptions<BackendSupervisorOptions>>().Value;

        Assert.Equal("127.0.0.1", options.BindAddress);
        Assert.Equal(18333, options.Port);

        // One budget: Chaos:BackendStartTimeout is the supervisor's startup gate.
        Assert.Equal(TimeSpan.FromSeconds(7), options.StartupGate);

        // One driver: the gateway's hosted service, not the supervisor's own,
        // so start happens after route validation and setup and within budget.
        Assert.False(options.RegisterHostedService);
        Assert.DoesNotContain(
            factory.Services.GetServices<Microsoft.Extensions.Hosting.IHostedService>(),
            service => service is BackendSupervisorHostedService);
    }

    [Fact]
    public async Task Supervision_off_keeps_the_no_op_fallback_and_says_so()
    {
        // The factory default: Chaos:SuperviseBackend=false. The gateway must
        // then fall back to the no-op supervisor rather than fail, and report
        // the fact rather than a comforting state.
        await using var stub = await StubBackend.StartAsync();
        using var factory = ChaosHostFactory.For(stub).Build();
        using var client = factory.CreateClient();

        Assert.IsType<NullBackendSupervisor>(factory.Services.GetRequiredService<IBackendSupervisor>());

        var supervisor = (await (await client.GetAsync("/host/info")).ReadJsonAsync()).GetProperty("supervisor");
        Assert.False(supervisor.GetProperty("registered").GetBoolean());
        Assert.Equal("Unknown", supervisor.GetStringProperty("state"));
        Assert.Equal(JsonValueKind.Null, supervisor.GetProperty("runtime").ValueKind);
    }
}
