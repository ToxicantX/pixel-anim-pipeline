using Godot;
using System;
using System.Threading;
using System.Threading.Tasks;

namespace PixelPipeline.Tests;

public partial class AcceptanceRunner : Node
{
    private const int TimeoutMilliseconds = 10000;

    public override async void _Ready()
    {
        GD.Print("[AcceptanceRunner] Starting test suite...");
        int exitCode = 1;

        try
        {
            var cts = new CancellationTokenSource();
            var runTask = RunTestsAsync(cts.Token);
            var timeoutTask = Task.Delay(TimeoutMilliseconds);

            var finishedTask = await Task.WhenAny(runTask, timeoutTask);
            if (finishedTask == timeoutTask)
            {
                cts.Cancel();
                GD.PrintErr($"[AcceptanceRunner] TIMEOUT: Tests did not finish within {TimeoutMilliseconds}ms.");
                exitCode = 1;
            }
            else
            {
                exitCode = await runTask;
            }
        }
        catch (Exception ex)
        {
            GD.PrintErr($"[AcceptanceRunner] UNHANDLED EXCEPTION: {ex}");
            exitCode = 1;
        }

        GD.Print($"[AcceptanceRunner] Exiting with code {exitCode}");
        GetTree().Quit(exitCode);
    }

    private async Task<int> RunTestsAsync(CancellationToken ct)
    {
        bool controllerOnly = Array.IndexOf(OS.GetCmdlineUserArgs(), "--controller-only") >= 0;
        int builderPassed = 0;
        if (!controllerOnly)
        {
            GD.Print("[AcceptanceRunner] Running BuilderChecks...");
            builderPassed = BuilderChecks.Run();
            GD.Print($"[AcceptanceRunner] BuilderChecks passed count: {builderPassed}");
            if (builderPassed <= 0)
            {
                GD.PrintErr($"[AcceptanceRunner] BuilderChecks failed or returned non-positive count: {builderPassed}");
                return 1;
            }
        }
        else
        {
            GD.Print("[AcceptanceRunner] BuilderChecks skipped for focused controller verification.");
        }

        ct.ThrowIfCancellationRequested();

        GD.Print("[AcceptanceRunner] Running ControllerChecks...");
        int controllerPassed = await ControllerChecks.Run(this);
        GD.Print($"[AcceptanceRunner] ControllerChecks passed count: {controllerPassed}");

        if (controllerPassed <= 0)
        {
            GD.PrintErr($"[AcceptanceRunner] ControllerChecks failed or returned non-positive count: {controllerPassed}");
            return 1;
        }

        GD.Print($"[AcceptanceRunner] ALL CHECKS PASSED: builder={builderPassed}, controller={controllerPassed}");
        return 0;
    }
}
