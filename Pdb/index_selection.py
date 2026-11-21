"""Workload-based index selection (The rule that picks B+ Tree or LSM from the workload counts.)"""

from dataclasses import dataclass

MIN_SAMPLE = 20
RANGE_SHARE = 0.30
WRITE_SHARE = 0.60

@dataclass
class WorkloadProfile:
    point_reads: int = 0
    range_reads: int = 0
    writes: int = 0

    @property
    def total(self):
        return self.point_reads + self.range_reads + self.writes

    @property
    def reads(self):
        return self.point_reads + self.range_reads


def choose_index(p: WorkloadProfile):
    """Return (kind, reason) with kind in {'btree','lsm'}."""
    if p.total < MIN_SAMPLE:
        return "btree", f"only {p.total} observed ops (< {MIN_SAMPLE}); default to B+ Tree"
    if p.reads and p.range_reads / p.reads >= RANGE_SHARE:
        return "btree", f"range reads are {p.range_reads / p.reads:.0%} of reads (>= {RANGE_SHARE:.0%})"
    if p.writes / p.total >= WRITE_SHARE:
        return "lsm", f"writes are {p.writes / p.total:.0%} of operations (>= {WRITE_SHARE:.0%}) and ranges are rare"
    return "btree", f"read-mostly/balanced workload ({p.writes / p.total:.0%} writes)"
