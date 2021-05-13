import functools
import io
import itertools
import os
import time

import anyio
import attr
import prompt_toolkit
import prompt_toolkit.buffer
import prompt_toolkit.input
import prompt_toolkit.key_binding
import prompt_toolkit.keys
import prompt_toolkit.layout.containers
import prompt_toolkit.layout.layout
import rich
import rich.console
import rich.layout
import rich.live
import rich.table

import plotman.configuration
import plotman.job
import plotman.manager
import plotman.plot_util
import plotman.reporting


def with_rich():
    config_path = plotman.configuration.get_path()
    config_text = plotman.configuration.read_configuration_text(config_path)
    cfg = plotman.configuration.get_validated_configs(config_text, config_path)

    tmp_prefix = os.path.commonpath(cfg.directories.tmp)
    dst_prefix = os.path.commonpath(cfg.directories.dst)

    overall = rich.layout.Layout('overall')
    rows = [
        header_layout,
        plots_layout,
        disks_layout,
        archive_layout,
        logs_layout,
    ] = [
        rich.layout.Layout(name='header'),
        rich.layout.Layout(name='plots'),
        rich.layout.Layout(name='disks'),
        rich.layout.Layout(name='archive'),
        rich.layout.Layout(name='logs'),
    ]
    overall.split_column(*rows)

    disks_layouts = [
        tmp_layout,
        dst_layout,
    ] = [
        rich.layout.Layout(name='tmp'),
        rich.layout.Layout(name='dst'),
    ]

    disks_layout.split_row(*disks_layouts)

    jobs = []

    prompt_toolkit_input = prompt_toolkit.input.create_input()
    with prompt_toolkit_input.raw_mode():
        with rich.live.Live(overall, auto_refresh=False) as live:
            for i in itertools.count():
                header_layout.update(str(i))

                jobs = plotman.job.Job.get_running_jobs(
                    cfg.directories.log,
                    cached_jobs=jobs,
                )
                jobs_data = build_jobs_data(
                    jobs=jobs,
                    dst_prefix=dst_prefix,
                    tmp_prefix=tmp_prefix,
                )

                jobs_table = build_jobs_table(jobs_data=jobs_data)
                plots_layout.update(jobs_table)

                tmp_data = build_tmp_data(
                    jobs=jobs,
                    dir_cfg=cfg.directories,
                    sched_cfg=cfg.scheduling,
                    prefix=tmp_prefix,
                )

                tmp_table = build_tmp_table(tmp_data=tmp_data)
                tmp_layout.update(tmp_table)

                live.refresh()
                for _ in range(10):
                    keys = prompt_toolkit_input.read_keys()
                    quit_keys = {'q', prompt_toolkit.keys.Keys.ControlC}
                    if any(key.key in quit_keys for key in keys):
                        return
                    time.sleep(0.1)


async def with_prompt_toolkit():
    config_path = plotman.configuration.get_path()
    config_text = plotman.configuration.read_configuration_text(config_path)
    cfg = plotman.configuration.get_validated_configs(config_text, config_path)

    tmp_prefix = os.path.commonpath(cfg.directories.tmp)
    dst_prefix = os.path.commonpath(cfg.directories.dst)

    header_buffer = prompt_toolkit.layout.controls.FormattedTextControl()
    plots_buffer = prompt_toolkit.layout.controls.FormattedTextControl()
    disks_buffer = prompt_toolkit.layout.controls.FormattedTextControl()
    rows = [
        header_window,
        plots_window,
        disks_window,
        archive_window,
        logs_window,
    ] = [
        prompt_toolkit.layout.containers.Window(content=header_buffer),
        prompt_toolkit.layout.containers.Window(content=plots_buffer),
        prompt_toolkit.layout.containers.Window(content=disks_buffer),
        prompt_toolkit.layout.containers.Window(),
        prompt_toolkit.layout.containers.Window(),
    ]

    root_container = prompt_toolkit.layout.containers.HSplit(rows)

    layout = prompt_toolkit.layout.Layout(root_container)

    key_bindings = prompt_toolkit.key_binding.KeyBindings()

    application = prompt_toolkit.Application(
        layout=layout,
        full_screen=True,
        key_bindings=key_bindings,
    )

    rich_console = rich.console.Console()

    jobs = []

    async with anyio.create_task_group() as task_group:
        key_bindings.add('q')(exit_key_binding)
        key_bindings.add('c-c')(exit_key_binding)

        task_group.start_soon(functools.partial(
            cancel_after_application,
            application=application,
            cancel_scope=task_group.cancel_scope,
        ))

        for i in itertools.count():
            header_buffer.text = str(i)

            jobs = plotman.job.Job.get_running_jobs(
                cfg.directories.log,
                cached_jobs=jobs,
            )
            jobs_data = build_jobs_data(
                jobs=jobs,
                dst_prefix=dst_prefix,
                tmp_prefix=tmp_prefix,
            )

            jobs_table = build_jobs_table(jobs_data=jobs_data)
            plots_buffer.text = capture_rich(jobs_table, console=rich_console)

            tmp_data = build_tmp_data(
                jobs=jobs,
                dir_cfg=cfg.directories,
                sched_cfg=cfg.scheduling,
                prefix=tmp_prefix,
            )

            tmp_table = build_tmp_table(tmp_data=tmp_data)
            disks_buffer.text = capture_rich(tmp_table, console=rich_console)

            application.invalidate()
            await anyio.sleep(1)


async def cancel_after_application(application, cancel_scope):
    await application.run_async()
    cancel_scope.cancel()


def exit_key_binding(event):
    event.app.exit()


def capture_rich(*objects, console):
    with console.capture() as capture:
        console.print(*objects)

    return prompt_toolkit.ANSI(capture.get())


def row_ib(name):
    return attr.ib(converter=str, metadata={'name': name})


@attr.frozen
class JobRow:
    plot_id: str = row_ib(name='plot id')
    k: str = row_ib(name='k')
    tmp_path: str = row_ib(name='tmp')
    dst: str = row_ib(name='dst')
    wall: str = row_ib(name='wall')
    phase: str = row_ib(name='phase')
    tmp_usage: str = row_ib(name='tmp')
    pid: str = row_ib(name='pid')
    stat: str = row_ib(name='stat')
    mem: str = row_ib(name='mem')
    user: str = row_ib(name='user')
    sys: str = row_ib(name='sys')
    io: str = row_ib(name='io')

    @classmethod
    def from_job(cls, job, dst_prefix, tmp_prefix):
        self = cls(
            plot_id=job.plot_id[:8],
            k=job.k,
            tmp_path=plotman.reporting.abbr_path(job.tmpdir, tmp_prefix),
            dst=plotman.reporting.abbr_path(job.dstdir, dst_prefix),
            wall=plotman.plot_util.time_format(job.get_time_wall()),
            phase=plotman.reporting.phase_str(job.progress()),
            tmp_usage=plotman.plot_util.human_format(job.get_tmp_usage(), 0),
            pid=job.proc.pid,
            stat=job.get_run_status(),
            mem=plotman.plot_util.human_format(job.get_mem_usage(), 1),
            user=plotman.plot_util.time_format(job.get_time_user()),
            sys=plotman.plot_util.time_format(job.get_time_sys()),
            io=plotman.plot_util.time_format(job.get_time_iowait())
        )

        return self


def build_jobs_data(jobs, dst_prefix, tmp_prefix):
    sorted_jobs = sorted(jobs, key=plotman.job.Job.get_time_wall)

    jobs_data = [
        JobRow.from_job(job=job, dst_prefix=dst_prefix, tmp_prefix=tmp_prefix)
        for index, job in enumerate(sorted_jobs)
    ]

    return jobs_data


def build_jobs_table(jobs_data):
    table = rich.table.Table(box=None, header_style='reverse')

    table.add_column('#')

    for field in attr.fields(JobRow):
        table.add_column(field.metadata['name'])

    for index, row in enumerate(jobs_data):
        table.add_row(str(index), *attr.astuple(row))

    return table


@attr.frozen
class TmpRow:
    path: str = row_ib(name='tmp')
    ready: bool = row_ib(name='ready')
    phases: list[plotman.job.Phase] = row_ib(name='phases')

    @classmethod
    def from_tmp(cls, dir_cfg, jobs, sched_cfg, tmp, prefix):
        phases = sorted(plotman.job.job_phases_for_tmpdir(d=tmp, all_jobs=jobs))
        tmp_suffix = plotman.reporting.abbr_path(path=tmp, putative_prefix=prefix)
        ready = plotman.manager.phases_permit_new_job(
            phases=phases,
            d=tmp_suffix,
            sched_cfg=sched_cfg,
            dir_cfg=dir_cfg,
        )
        self = cls(
            path=tmp_suffix,
            ready='OK' if ready else '--',
            phases=plotman.reporting.phases_str(phases=phases, max_num=5),
        )
        return self


def build_tmp_data(jobs, dir_cfg, sched_cfg, prefix):
    rows = [
        TmpRow.from_tmp(
            dir_cfg=dir_cfg,
            jobs=jobs,
            sched_cfg=sched_cfg,
            tmp=tmp,
            prefix=prefix,
        )
        for tmp in sorted(dir_cfg.tmp)
    ]

    return rows


def build_tmp_table(tmp_data):
    table = rich.table.Table(box=None, header_style='reverse')

    for field in attr.fields(TmpRow):
        table.add_column(field.metadata['name'])

    for row in tmp_data:
        table.add_row(*attr.astuple(row))

    return table
