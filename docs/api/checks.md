# Checks

Functions that read only a finished run's files — no cluster needed, so
any run can be re-examined later. Every verdict is three-way (violated / clean /
insufficient evidence) and a violation carries the recorded operations that
prove it. Before trusting a clean verdict, read
[what the checks can and cannot see](../checks.md).

## Verdicts

::: hokea.check
    options:
      members: false
      show_root_full_path: true
      heading_level: 3

::: hokea.check.Verdict
    options:
      heading_level: 3

::: hokea.check.InsufficientEvidence
    options:
      heading_level: 3

## The five checks

::: hokea.check.converged
    options:
      heading_level: 3

::: hokea.check.acked_visible
    options:
      heading_level: 3

::: hokea.check.session
    options:
      heading_level: 3

::: hokea.check.event_join
    options:
      heading_level: 3

::: hokea.check.availability
    options:
      heading_level: 3

## Availability results

`availability()` returns an `Availability`; its windows and sides are what
your test asserts against:

```python
a = check.availability(h.history, h.faults)
side = a.window("partition").side("majority")
assert side.success_rate > 0.9
```

::: hokea.check.Availability
    options:
      heading_level: 3
      members:
        - window

::: hokea.check.Window
    options:
      heading_level: 3

::: hokea.check.SideStats
    options:
      heading_level: 3
