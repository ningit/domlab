## Improving feedback on student submissions to online judges with code analysis and instrumentation

This repository includes two Python packages aimed at improving feedback for students submitting problem assignments to online judges in programming courses.
* `codegavel` is a library to apply code analysis and instrumentation checks on problem assignments. It relies on [clang-tidy](https://clang.llvm.org/extra/clang-tidy/) for out-of-the-box static analyses, [libclang](https://clang.llvm.org/docs/LibClang.html) for custom code analyses, [Google sanitizers](https://github.com/google/sanitizers) for runtime checks by instrumentation, and [systemd](https://systemd.io/) for executing submissions against test cases in a sandboxed environment while measuring resource usage.
* `domlab` is a web service that tracks the event feeds of several [DOMjudge](https://www.domjudge.org/) contests while running `codegavel` checks on their submissions. On the one hand, information is presented to the teacher through a web interface. On the other hand, an API endpoint is provided to integrate the diagnostics for the students in the DOMjudge interface.

### Using `domlab`

In order to use `domlab`, a DOMjudge instance to follow, a MongoDB database, and a configuration file telling where to find those are required. Configuration files can be written in JSON, YAML, or TOML. An example configuration file is available at [`util/config.toml`](domlab/util/config.toml). Then the program can be executed as follows:
```
python3 -m domlab -c /etc/domlab/config.toml -l /run/domlab/domlab.sock
```
where `-c` indicates the path to the configuration file, and `-l` where to listen for connections (a Unix socket in this case, but it can also be an `address:port` pair). More options are listed when running the command with the `--help` flag.

### Using `codegavel`

`codegavel` can be used as a library to run the desired checks on a submission. For example,
```python
from codegavel import Toolchain
from pathlib import Path

# Create a toolchain
tc = Toolchain(compiler_args=('-DDOMJUDGE',))

# Relevant directories
src_dir, out_dir, test_cases = # as desired

# Create a new submisison object to run checks
subm = tc.new_submission(src_dir, output_dir=out_dir)

# Run out-of-the-box static analyses
subm.check_static()
# Run custom static analyses
subm.check_custom()

# Run test cases with instrumentation
for case in test_cases.glob('*.in'):
	print(subm.check_output(case, case.with_suffix('.out'),
	                        out_dir / f'{case.stem}.out',
	                        instrument=True))

# Obtain a summary of the results
print(subm.summary())
```

### Dependencies

* `codegavel` depends on the aforementioned tools for their corresponding analyses. Unless only static analysis is desired, a recent version of Linux and `systemd` (and its [`pystemd`](https://github.com/systemd/pystemd) library) is necessary for accurate time and memory usage measurements. Moreover, for sandboxing without superuser privileges, user-level namespaces should be enabled in the Linux kernel. [`pyyaml`](https://github.com/yaml/pyyaml) is required for processing `clang-tidy` output.
* `domlab` relies on [`tornado`](https://www.tornadoweb.org/en/stable/), [`motor`](https://github.com/mongodb/motor), [`httpx`](https://github.com/encode/httpx), [`lxml`](https://lxml.de/), and `codegavel` as required dependencies. `pyyaml` and Python 3.12 are only required in order to load settings from YAML or TOML files.

### About

These tools are part of the Innova-Docencia project *Mejora de la retroalimentación de los jueces en línea mediante análisis estático e instrumentación* (number 71 of the academic year 2024-25) by Universidad Complutense de Madrid.
