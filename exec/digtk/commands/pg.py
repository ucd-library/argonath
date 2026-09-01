"""
digtk public-dc pg -- write directly into fin's Postgres workflow table.

Subcommands:
  mark  -- write a workflow-completion marker row for an item's binaries

See digtk.lib.pg_workflow's module docstring for why this exists alongside
digtk.lib.gcs_sync's GCS-marker-upload path (`digtk public-dc gcs sync`).
"""
import enum
from typing import Annotated

import typer

from digtk.lib import pg_workflow, utils
from digtk.lib.config import CONFIG, GcsBucketOption

app = typer.Typer(help="Direct fin Postgres workflow-table commands.", no_args_is_help=True)

_ERRORS = (LookupError, OSError, RuntimeError, ValueError)


# Mirrors digtk.lib.pg_workflow.STATES -- a real Enum (rather than a plain
# Typer Choice) so `--help` and shell completion show the fixed list of
# fin_workflow_state values directly.
State = enum.Enum("State", {s: s for s in pg_workflow.STATES})


@app.command()
def mark(
    state: Annotated[State, typer.Argument(help="Workflow state to write.")],
    ark: Annotated[str, typer.Argument(help="Item ARK identifier.")],
    bucket: GcsBucketOption = CONFIG["gcs"]["bucket"],
):
    """Write a workflow-completion marker directly into fin's
    workflow.workflow table for every image/PDF/video binary on this item,
    so fin's createWorkflow() sees it as already done and skips re-running
    its Cloud Workflow derivative pipeline.
    """
    try:
        utils.log(f"Marking {ark} as '{state.value}' in fin's workflow table")
        result = pg_workflow.mark(ark, state.value, opts={"bucket": bucket})
        for fmt, marked in result["marked"].items():
            utils.log(f"  {fmt}: {', '.join(marked)}")
        utils.result(result)
    except _ERRORS as e:
        utils.error(str(e))
