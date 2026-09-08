# hokea

hokea is the testing framework for this course's distributed-systems
project. You bring a small distributed system; hokea runs it as a fleet of
nodes (containers on your laptop, or pods on the class cluster), then
kills, freezes, partitions, and slows those nodes while clients drive
load, and records everything. Your tests then compare what you claim your
system does against what it actually did.

**Start with the [introductory lab](lab.md).** It takes you from an empty
directory to a running fault test, then to the class cluster, in four acts.

The other pages, for when you are building your own system:

- [Adapters](adapters.md) — the roughly 40 lines that connect hokea to your server; a mistake here can corrupt every verdict, so read it before writing one.
- [Artifacts](artifacts.md) — the files every run records, and what each field means.
- [Checks](checks.md) — the five shipped checks: what question each answers, and what it cannot see.
- [Data & durability](data-and-durability.md) — what happens to a node's data when it is killed, where nodes may write, and how to give them a dataset.
- [Testing your claims](testing-your-claims.md) — turning each claim in your project proposal into an experiment that could prove it wrong.
- [Datasets](datasets.md) — the ready-made course dataset images, and how to publish your own.
- [API reference](api/index.md) — every class and function, generated from the source.
