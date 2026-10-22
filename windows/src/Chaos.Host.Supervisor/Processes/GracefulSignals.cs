using System.ComponentModel;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Runtime.Versioning;

namespace Chaos.Host.Supervisor.Processes;

/// <summary>
/// Best-effort "please exit" signals. Every method returns a verdict plus a
/// reason, because "we asked nicely" and "there was nobody to ask" must not
/// look the same in a log.
/// </summary>
/// <remarks>
/// This is the SECOND of the supervisor's three stop paths. The first is the
/// backend's own loopback stop endpoint (<c>POST /_supervisor/stop</c> with a
/// per-launch secret, <see cref="Health.BackendStopContract"/>), which works
/// in every run mode because it needs no console and no signal; a signal is
/// sent only when that endpoint did not take the request. The third is the
/// Job Object, after <c>GracefulShutdownTimeout</c>. See
/// <c>BackendSupervisor.StopChildAsync</c>.
/// </remarks>
internal static partial class GracefulSignals
{
    /// <summary>
    /// Sends the platform's polite stop signal to <paramref name="processId"/>.
    /// </summary>
    public static bool TryRequestStop(int processId, out string detail)
    {
        if (OperatingSystem.IsWindows())
        {
            return WindowsConsoleBreak.TrySend(processId, out detail);
        }

        return UnixSignals.TrySendTerm(processId, out detail);
    }

    /// <summary>
    /// CTRL_BREAK to a child's console group.
    /// </summary>
    /// <remarks>
    /// <para>
    /// Windows has no SIGTERM. The only in-band "please stop" for a console
    /// process is a console control event, and it can only be delivered to a
    /// console the sender is attached to.
    /// </para>
    /// <para>
    /// Two cases:
    /// </para>
    /// <list type="bullet">
    /// <item>
    /// <b>Service mode</b> — the service has no console and starts the child
    /// with <c>CreateNoWindow</c>, so the child has none either.
    /// <c>AttachConsole</c> fails and there is nothing to send. We say so.
    /// This is why the loopback stop endpoint is tried first: a service's
    /// backend is normally stopped by that request, answers <c>202</c>, and
    /// shuts uvicorn down itself so its lifespan cleanup runs; this branch is
    /// reached only when the endpoint did not take the request (a backend
    /// older than the contract, or one that is alive but not listening), and
    /// then the caller escalates to the Job Object after the bounded wait.
    /// Giving the child a console group of its own (raw <c>CreateProcessW</c>
    /// with <c>CREATE_NEW_PROCESS_GROUP</c>) would make this branch useful in
    /// service mode too; it is not done, because the endpoint covers the case.
    /// Only a run as a real Windows service proves uvicorn's handlers execute
    /// on that path; see docs/advanced-windows-packaging.md, "Open items".
    /// </item>
    /// <item>
    /// <b>Console mode</b> (<c>--console</c>) — the child shares our console.
    /// Broadcasting a break event there would signal the operator's own
    /// console session too, so we decline and let the caller escalate.
    /// </item>
    /// </list>
    /// </remarks>
    [SupportedOSPlatform("windows")]
    internal static partial class WindowsConsoleBreak
    {
        private const uint CtrlBreakEvent = 1;

        public static bool TrySend(int processId, out string detail)
        {
            if (GetConsoleWindow() != IntPtr.Zero)
            {
                detail =
                    "this process owns a console, and the child shares it: a CTRL_BREAK would hit the " +
                    "operator's own session as well, so it was not sent";
                return false;
            }

            if (!AttachConsole((uint)processId))
            {
                detail =
                    $"AttachConsole({processId.ToString(CultureInfo.InvariantCulture)}) failed " +
                    $"({LastError()}): the child has no console, so Windows offers no graceful signal";
                return false;
            }

            var handlerDisabled = SetConsoleCtrlHandler(IntPtr.Zero, add: true);
            try
            {
                if (!GenerateConsoleCtrlEvent(CtrlBreakEvent, 0))
                {
                    detail = $"GenerateConsoleCtrlEvent(CTRL_BREAK) failed ({LastError()})";
                    return false;
                }

                detail = "CTRL_BREAK sent to the child's console group";
                return true;
            }
            finally
            {
                FreeConsole();
                if (handlerDisabled)
                {
                    SetConsoleCtrlHandler(IntPtr.Zero, add: false);
                }
            }
        }

        private static string LastError()
        {
            var code = Marshal.GetLastWin32Error();
            return $"Win32 error {code.ToString(CultureInfo.InvariantCulture)}: {new Win32Exception(code).Message}";
        }

        [LibraryImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static partial bool AttachConsole(uint processId);

        [LibraryImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static partial bool FreeConsole();

        [LibraryImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static partial bool GenerateConsoleCtrlEvent(uint controlEvent, uint processGroupId);

        [LibraryImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static partial bool SetConsoleCtrlHandler(IntPtr handlerRoutine, [MarshalAs(UnmanagedType.Bool)] bool add);

        [LibraryImport("kernel32.dll")]
        private static partial IntPtr GetConsoleWindow();
    }

    /// <summary>
    /// SIGTERM. .NET's <c>Process.Kill()</c> is SIGKILL, which uvicorn cannot
    /// catch, so the platform would never run its shutdown handlers. This is
    /// the path <c>SystemProcessLauncherTests</c> (Chaos.Host.Supervisor.Tests)
    /// exercises against a real <c>python3</c> child that traps the signal:
    /// once directly through <see cref="SystemProcessLauncher"/>, and once
    /// through the whole supervisor, asserting the child's own exit code and
    /// that no forced termination was recorded.
    /// </summary>
    [UnsupportedOSPlatform("windows")]
    internal static partial class UnixSignals
    {
        private const int Sigterm = 15;

        public static bool TrySendTerm(int processId, out string detail)
        {
            if (Kill(processId, Sigterm) == 0)
            {
                detail = $"SIGTERM sent to pid {processId.ToString(CultureInfo.InvariantCulture)}";
                return true;
            }

            var error = Marshal.GetLastPInvokeError();
            detail =
                $"kill({processId.ToString(CultureInfo.InvariantCulture)}, SIGTERM) failed with errno " +
                error.ToString(CultureInfo.InvariantCulture);
            return false;
        }

        [LibraryImport("libc", EntryPoint = "kill", SetLastError = true)]
        private static partial int Kill(int pid, int signal);
    }
}
