# Workload & runs

`run_workload` puts concurrent clients on the cluster through
[your adapter and ops generator](../adapters.md), executes the fault schedule,
and writes a run directory; `record_run` does the same for systems with no
request/response load (batch jobs, training). Both return a `RunHandle`,
which reads the [run's files](../artifacts.md) back for
[checks](checks.md) and your own analysis.

## Driving load

::: hokea.workload
    options:
      members: false
      show_root_full_path: true
      heading_level: 3

An `Op` is what your ops generator yields; a `Result` is what your adapter
returns. The fields, and the rules for choosing a `Result`'s
`ok` / `rejected` / `unknown` outcome, are documented where you write that
code: the [adapters guide](../adapters.md).

::: hokea.workload.Op
    options:
      heading_level: 3

::: hokea.workload.Result
    options:
      heading_level: 3

::: hokea.workload.http_result
    options:
      heading_level: 3

::: hokea.workload.Script
    options:
      heading_level: 3
      members:
        - do

::: hokea.workload.run_workload
    options:
      heading_level: 3

::: hokea.workload.record_run
    options:
      heading_level: 3

## Reading a run back

::: hokea.runs
    options:
      members: false
      show_root_full_path: true
      heading_level: 3

::: hokea.runs.RunHandle
    options:
      heading_level: 3
      members:
        - history
        - faults
        - events
        - states
        - metadata
        - report
