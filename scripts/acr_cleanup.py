#!/usr/bin/env python3
"""
Periodic Azure Container Registry cleanup for baipacr.

baipacr is a Standard-SKU registry (100 GiB included). ACR's built-in
retention policy for untagged manifests is Premium-only, so cleanup is driven
externally with `acr purge`, run as an on-demand ACR Task.

--- Retention is count based, not age based ---

Keep the N most recently updated tags per repository; delete the rest whatever
their age. See DEFAULT_RULES for how that maps onto acr purge.

--- Why a plain `acr purge` is unsafe here ---

  1. Live deployments pin very old tags. dev/edai-speech2text has been running
     2ba4009-20260507 for months, and it is not among the newest few, so
     nothing but the lock list keeps it alive.

  2. The registry path does not follow the values directory. rke2-baip/prod/
     edai-agentic-ai-scraper.yaml deliberately points at a *dev* image because
     that service's prod code has not shipped, so a dev-scoped purge can break
     production.

So the safe-list is derived from what is actually deployed, never from age or
path convention. `acr purge` skips artifacts with delete-enabled=false unless
--include-locked is passed (this script never passes it), which makes locking
an exact, registry-enforced safe-list.

--- Phases (order matters) ---

  1. unlock   Clear delete-enabled=false left over from the previous run.
              Without this, locks accumulate every run until everything is
              locked and purge silently becomes a no-op -- a failure that
              looks like success.

  2. lock     Union of two sources:
                a) image.repository + image.tag in <values-dir>/*/*.yaml
                   (what ArgoCD intends)  -- REQUIRED
                b) images on running pods via kubectl
                   (what is actually up)  -- best-effort
              They diverge during a failed sync or a manual rollback, so the
              union closes that window.

  3. purge    az acr run --cmd 'acr purge ...'. Locked artifacts are skipped.

Dying between phases is safe: phase 3 never runs, so nothing is deleted.

Usage:
  # Report what would happen, delete nothing (always start here):
  python scripts/acr_cleanup.py --registry baipacr \\
    --values-dir /path/to/baip-argocd-helm/rke2-baip --dry-run

  # Real run:
  python scripts/acr_cleanup.py --registry baipacr \\
    --values-dir /path/to/baip-argocd-helm/rke2-baip

  # Different retention (<repo-regex>:<how many newest tags to keep>):
  python scripts/acr_cleanup.py --registry baipacr --values-dir ... \\
    --rule 'dev/.*:3' --rule 'prod/.*:5'

Requirements:
  Azure CLI, authenticated. The principal needs AcrPush + AcrDelete, plus
  permission to start an ACR Task run (scheduleRun/action).
  kubectl is optional; without it, phase 2 falls back to values files only.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import time

# Retention rules as "<repo-regex>:<keep>" -- purely count based. Keep the N
# most recently updated tags per repository and delete the rest, regardless of
# age.
#
# acr purge has no count-only mode: --filter and --ago are both required. So
# age is neutralised with `--ago 0d`, which the docs define as matching images
# of all ages, leaving --keep as the only retention control.
#
# IMPORTANT consequence: with no age floor, EVERY tag is a deletion candidate.
# Under the previous 7d/90d rules a recently pushed image was safe purely
# because it was new. It no longer is. The lock list is now the only thing
# protecting a deployed image that has fallen outside the newest N, so phase 2
# is load-bearing in a way it was not before.
DEFAULT_RULES = ["dev/.*:3", "prod/.*:5"]

# az acr run's default on-demand timeout is 600s, which silently truncates a
# large purge and deletes only a subset.
PURGE_TIMEOUT = 3600


def resolve(cmd):
    """Resolve argv[0] on PATH.

    On Windows `az` and `kubectl` are .cmd shims, which CreateProcess will not
    launch by bare name. shutil.which finds them; on Linux runners this is a
    no-op.
    """
    exe = shutil.which(cmd[0])
    if exe is None:
        raise FileNotFoundError(cmd[0])
    return [exe] + cmd[1:]


def run(cmd, check=True, capture=True, retries=0):
    """Run a command, returning (stdout, returncode). Raises when check=True.

    Retries exist as cheap insurance for transient ACR failures on a run that
    makes one call per repository: a single blip would otherwise abort
    everything partway through. They are opt-in because they are only safe for
    idempotent calls (list, show, attribute update) -- never for starting a
    purge.

    Note: an earlier version of this comment blamed ACR throttling, based on a
    local sweep where 21 of 22 repository queries failed. That diagnosis was
    wrong. The real cause was `az ... -o tsv` emitting CRLF on Windows, so
    every repository name except the last carried a trailing \\r and came back
    "The requested data does not exist". This script is unaffected because it
    parses JSON, not tsv -- but do not cite that incident as evidence of
    throttling.
    """
    attempt, delay = 0, 2.0
    while True:
        proc = subprocess.run(
            resolve(cmd),
            check=False,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
        )
        if proc.returncode == 0:
            return (proc.stdout or "").strip(), 0

        if attempt < retries:
            attempt += 1
            print("    retry %d/%d after failure: %s"
                  % (attempt, retries, (proc.stderr or "").strip().splitlines()[:1]))
            time.sleep(delay)
            delay *= 2
            continue

        if check:
            raise RuntimeError(
                "command failed (%d): %s\n%s"
                % (proc.returncode, " ".join(cmd), (proc.stderr or "").strip())
            )
        return (proc.stdout or "").strip(), proc.returncode


def az_json(args, retries=4):
    out, _ = run(["az"] + args + ["-o", "json"], retries=retries)
    return json.loads(out) if out else []


def list_repositories(registry):
    return az_json(["acr", "repository", "list", "--name", registry])


def list_manifests(registry, repo):
    return az_json(
        ["acr", "manifest", "list-metadata", "--registry", registry, "--name", repo]
    )


def list_tags_detail(registry, repo):
    """Tags with their own changeableAttributes.

    Locks live at TAG level, not manifest level. `az acr repository update
    --image repo:tag --delete-enabled false` writes a tag attribute, and
    `az acr manifest list-metadata` does NOT reflect it -- the same image can
    read deleteEnabled=false as a tag and true as a manifest. Reading the wrong
    one makes the unlock phase a silent no-op, which lets stale locks
    accumulate until purge stops deleting anything.
    """
    return az_json(
        ["acr", "repository", "show-tags", "--name", registry,
         "--repository", repo, "--detail"]
    )


# --------------------------------------------------------------------------
# Phase 2 input: what is actually deployed
# --------------------------------------------------------------------------


def parse_image_ref(ref, registry):
    """'baipacr.azurecr.io/dev/foo:abc' -> ('dev/foo', 'abc'), else None.

    The registry path is read literally. Never infer it from the values
    directory name -- prod/ files legitimately reference dev/ images.
    """
    if not ref:
        return None
    prefix = registry + ".azurecr.io/"
    if not ref.startswith(prefix):
        return None
    rest = ref[len(prefix) :]
    if "@" in rest or ":" not in rest:
        return None  # digest pin or untagged; nothing to lock by tag
    repo, tag = rest.rsplit(":", 1)
    return (repo, tag) if repo and tag else None


def in_use_from_values(values_dir, registry):
    """Read image.repository/image.tag from <values-dir>/*/*.yaml.

    Deliberately a line scanner, not a YAML parse: it needs no PyYAML in the
    runner and these files have a fixed two-line image block.
    """
    import os
    import re

    found, parsed, failed = set(), 0, []
    repo_re = re.compile(r"^\s*repository:\s*(\S+)\s*$")
    tag_re = re.compile(r"^\s*tag:\s*[\"']?([^\"'\s]+)[\"']?\s*$")

    if not os.path.isdir(values_dir):
        raise RuntimeError("values dir not found: %s" % values_dir)

    for env in sorted(os.listdir(values_dir)):
        env_path = os.path.join(values_dir, env)
        if not os.path.isdir(env_path):
            continue
        for name in sorted(os.listdir(env_path)):
            if not name.endswith((".yaml", ".yml")):
                continue
            path = os.path.join(env_path, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    lines = fh.readlines()
            except OSError as exc:
                failed.append("%s: %s" % (path, exc))
                continue

            repo = tag = None
            for i, line in enumerate(lines):
                m = repo_re.match(line)
                if m and "azurecr.io" in m.group(1):
                    repo = m.group(1)
                    for nxt in lines[i + 1 : i + 3]:
                        t = tag_re.match(nxt)
                        if t:
                            tag = t.group(1)
                            break
                    break

            if repo and tag:
                ref = parse_image_ref("%s:%s" % (repo, tag), registry)
                if ref:
                    found.add(ref)
                    parsed += 1
                else:
                    failed.append("%s: unparseable image %s:%s" % (path, repo, tag))
            elif repo or tag:
                failed.append("%s: incomplete image block" % path)

    return found, parsed, failed


def in_use_from_cluster(registry):
    """Images on running pods. Best-effort: returns None if kubectl is absent
    or the API is unreachable, which must not fail the run."""
    jsonpath = (
        "{range .items[*]}{.spec.containers[*].image}{\"\\n\"}"
        "{.spec.initContainers[*].image}{\"\\n\"}{end}"
    )
    try:
        out, rc = run(
            ["kubectl", "get", "pods", "-A", "-o", "jsonpath=" + jsonpath],
            check=False,
        )
    except FileNotFoundError:
        return None
    if rc != 0:
        return None

    found = set()
    for token in out.replace(" ", "\n").splitlines():
        ref = parse_image_ref(token.strip(), registry)
        if ref:
            found.add(ref)
    return found


# --------------------------------------------------------------------------
# Phases
# --------------------------------------------------------------------------


def phase_unlock(registry, dry_run):
    """Clear delete-enabled=false so this run starts from a clean slate.

    Reads TAG-level attributes (see list_tags_detail). An earlier version read
    manifest metadata here and therefore never saw a single lock, reporting
    "unlocked: 0" forever while locks piled up -- the exact silent-no-op
    failure this phase exists to prevent.
    """
    print("\n=== phase 1: unlock ===")
    repos = list_repositories(registry)
    # One API call per repository, so this is the slowest phase and the one
    # most likely to look hung. Report progress rather than going silent.
    print("  scanning %d repositories for stale locks" % len(repos))
    unlocked = 0
    for i, repo in enumerate(repos, 1):
        print("  [%d/%d] %s" % (i, len(repos), repo))
        for tag in list_tags_detail(registry, repo):
            attrs = tag.get("changeableAttributes") or {}
            if attrs.get("deleteEnabled") is False:
                target = "%s:%s" % (repo, tag.get("name"))
                print("  unlock %s" % target)
                if not dry_run:
                    run(
                        [
                            "az", "acr", "repository", "update",
                            "--name", registry, "--image", target,
                            "--delete-enabled", "true",
                            "--write-enabled", "true",
                        ],
                        retries=4,
                    )
                unlocked += 1
    print("  unlocked: %d" % unlocked)
    return unlocked


def phase_lock(registry, in_use, dry_run):
    print("\n=== phase 2: lock in-use ===")
    locked = 0
    for repo, tag in sorted(in_use):
        target = "%s:%s" % (repo, tag)
        print("  lock %s" % target)
        if not dry_run:
            _, rc = run(
                [
                    "az", "acr", "repository", "update",
                    "--name", registry, "--image", target,
                    "--delete-enabled", "false",
                ],
                check=False,
                retries=4,
            )
            if rc != 0:
                # A referenced tag that no longer exists in ACR is a real
                # problem, but not a reason to skip cleanup entirely.
                print("    WARNING: could not lock %s" % target)
                continue
        locked += 1
    print("  locked: %d" % locked)
    return locked


def acr_run(registry, cmd):
    """Start an ACR Task run, translating the misleading RBAC error.

    If the principal lacks control-plane read on the registry, Azure reports
    the registry as missing rather than forbidden:

      The resource with name 'baipacr' and type
      'Microsoft.ContainerRegistry/registries' could not be found in
      subscription ...

    AcrPush grants only data-plane actions (pull/read, push/write), so a
    principal that can push images still cannot resolve the registry by name
    and never reaches scheduleRun. The registry is fine; the role is not.
    """
    try:
        out, _ = run(
            [
                "az", "acr", "run",
                "--registry", registry,
                "--timeout", str(PURGE_TIMEOUT),
                "--cmd", cmd,
                "/dev/null",
            ]
        )
    except RuntimeError as exc:
        if "could not be found in subscription" in str(exc):
            raise RuntimeError(
                "%s\n\n"
                "  HINT: this almost certainly means missing RBAC, not a missing registry.\n"
                "  '%s' exists but this principal cannot read it at the control plane.\n"
                "  AcrPush covers only pull/read + push/write (data plane).\n"
                "  Needs Microsoft.ContainerRegistry/registries/read and .../scheduleRun/action\n"
                "  -- see the 'ACR Purge Runner' role in\n"
                "  baip-infra-tools/terraform/11-az-github-fedarated-access.tf" % (exc, registry)
            )
        raise
    return out


def phase_purge(registry, rules, dry_run):
    print("\n=== phase 3: purge ===")
    outputs = []
    filters = []
    for rule in rules:
        try:
            pattern, keep = rule.split(":")
            int(keep)
        except ValueError:
            raise RuntimeError(
                "bad --rule %r, expected <regex>:<keep> (count only, no age)" % rule
            )
        filters.append((pattern, keep))

    for pattern, keep in filters:
        # --ago 0d matches every age, so --keep is the sole retention control.
        cmd = (
            "acr purge --filter '%s:.*' --ago 0d --keep %s --untagged"
            % (pattern, keep)
        )
        if dry_run:
            cmd += " --dry-run"
        print("  %s" % cmd)
        out = acr_run(registry, cmd)
        print(out)
        outputs.append(out)

    # Dangling manifests across every repository. Safe by construction: an
    # untagged manifest is not referenced by any tag.
    sweep = "acr purge --untagged-only"
    if dry_run:
        sweep += " --dry-run"
    print("  %s" % sweep)
    out = acr_run(registry, sweep)
    print(out)
    outputs.append(out)

    return purge_counts("\n".join(outputs))


def purge_counts(out):
    """Total deleted tags/manifests across every purge invocation.

    acr purge words its totals differently per mode:
      real     "Number of deleted tags: 4"
      dry-run  "Number of tags to be deleted: 4"
    """
    def total(kind):
        pat = r"Number of (?:deleted %s|%s to be deleted):\s*(\d+)" % (kind, kind)
        return sum(int(n) for n in re.findall(pat, out))

    return {"tags": total("tags"), "manifests": total("manifests")}


def registry_size(registry):
    """(used_bytes, limit_bytes), or None if unavailable."""
    try:
        usage = az_json(["acr", "show-usage", "--name", registry])
    except RuntimeError:
        return None
    # show-usage returns {"value": [...]}, not a bare list.
    if isinstance(usage, dict):
        usage = usage.get("value") or []
    for row in usage:
        if row.get("name") == "Size":
            return int(row["currentValue"]), int(row["limit"])
    return None


def gib(n):
    return n / 1073741824.0


def show_usage(registry, label):
    size = registry_size(registry)
    if size is None:
        return None
    cur, lim = size
    print(
        "%s: %.2f GiB used of %.2f GiB (%s)"
        % (label, gib(cur), gib(lim), "OVER QUOTA" if cur > lim else "ok")
    )
    return size


def write_summary(path, stats):
    """Render a GitHub step summary.

    Deliberately not `tail` of the log: the numbers that matter (what was kept,
    what was reclaimed) are spread across the whole run, and on an early
    failure there is no useful tail at all.
    """
    if not path:
        return

    ok = stats.get("error") is None
    mode = "dry run — nothing deleted" if stats.get("dry_run") else "live run"
    lines = [
        "## ACR cleanup — %s" % ("succeeded" if ok else "FAILED"),
        "",
        "`%s` · %s" % (stats.get("registry", "?"), mode),
        "",
    ]

    if not ok:
        lines += ["> [!CAUTION]", "> ```", ]
        lines += ["> " + ln for ln in str(stats["error"]).splitlines()]
        lines += ["> ```", ""]

    before, after = stats.get("before"), stats.get("after")
    if before and after:
        reclaimed = before[0] - after[0]
        lines += [
            "| | Used | Limit | |",
            "|---|---|---|---|",
            "| Before | %.2f GiB | %.2f GiB | %s |"
            % (gib(before[0]), gib(before[1]), "over quota" if before[0] > before[1] else "ok"),
            "| After | %.2f GiB | %.2f GiB | %s |"
            % (gib(after[0]), gib(after[1]), "over quota" if after[0] > after[1] else "ok"),
            "| **Reclaimed** | **%.2f GiB** | | |" % gib(reclaimed),
            "",
        ]

    counts = stats.get("counts") or {}
    lines += [
        "| Metric | Count |",
        "|---|---|",
        "| Images protected (locked) | %s |" % stats.get("locked", "-"),
        "| Stale locks cleared | %s |" % stats.get("unlocked", "-"),
        "| Tags %s | %s" % ("that would be deleted" if stats.get("dry_run") else "deleted",
                            "%s |" % counts.get("tags", "-")),
        "| Manifests %s | %s" % ("that would be deleted" if stats.get("dry_run") else "deleted",
                                 "%s |" % counts.get("manifests", "-")),
        "",
    ]

    safe = stats.get("safe_list") or []
    if safe:
        lines += [
            "<details><summary>Protected from deletion (%d deployed images)</summary>" % len(safe),
            "",
            "```",
        ]
        lines += ["%s:%s" % (r, t) for r, t in sorted(safe)]
        lines += ["```", "", "</details>", ""]

    warnings = stats.get("warnings") or []
    if warnings:
        lines += ["<details><summary>Warnings (%d)</summary>" % len(warnings), "", "```"]
        lines += warnings
        lines += ["```", "", "</details>", ""]

    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError as exc:
        print("WARNING: could not write summary: %s" % exc)


def main():
    ap = argparse.ArgumentParser(
        description="Lock deployed images, then purge everything else from ACR."
    )
    ap.add_argument("--registry", required=True, help="ACR name, e.g. baipacr")
    ap.add_argument(
        "--values-dir",
        required=True,
        help="argocd-helm cluster dir holding <env>/<service>.yaml, e.g. .../rke2-baip",
    )
    ap.add_argument(
        "--rule",
        action="append",
        dest="rules",
        metavar="REGEX:KEEP",
        help="Keep N newest tags per matching repo, repeatable (default: %s)"
        % " ".join(DEFAULT_RULES),
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Report only. Nothing is locked, unlocked, or deleted.",
    )
    ap.add_argument(
        "--skip-cluster",
        action="store_true",
        help="Do not consult kubectl; use values files as the only safe-list source.",
    )
    ap.add_argument(
        "--lock-only",
        action="store_true",
        help=(
            "Run the unlock and lock phases for real, then stop without purging. "
            "Use this to verify the safe-list actually protects live images: "
            "lock-only, then run the purge with --dry-run by hand and confirm no "
            "deployed tag appears in the delete list. A full --dry-run cannot "
            "prove this, because it never applies the locks."
        ),
    )
    ap.add_argument(
        "--summary-md",
        metavar="PATH",
        help=(
            "Append a markdown run summary to PATH (e.g. $GITHUB_STEP_SUMMARY). "
            "Written on failure too, so an early abort still reports why."
        ),
    )
    args = ap.parse_args()
    rules = args.rules or DEFAULT_RULES
    stats = {"registry": args.registry, "dry_run": args.dry_run, "error": None,
             "warnings": []}

    print("registry:   %s" % args.registry)
    print("values dir: %s" % args.values_dir)
    print("rules:      %s" % ", ".join(rules))
    print("dry run:    %s" % args.dry_run)
    stats["before"] = show_usage(args.registry, "\nbefore")

    # --- build the safe-list ------------------------------------------------
    from_values, parsed, failed = in_use_from_values(args.values_dir, args.registry)
    print("\nvalues files: %d image refs from %d files" % (len(from_values), parsed))
    for problem in failed:
        print("  WARNING: %s" % problem)
        stats["warnings"].append(problem)

    # An empty or partial safe-list combined with an aggressive purge is the
    # one combination that destroys a live environment. Refuse to continue.
    if failed:
        stats["error"] = (
            "ABORT: %d values file(s) could not be parsed. Refusing to purge with an "
            "incomplete safe-list." % len(failed)
        )
        write_summary(args.summary_md, stats)
        sys.exit(stats["error"])
    if not from_values:
        stats["error"] = "ABORT: safe-list is empty. Refusing to purge."
        write_summary(args.summary_md, stats)
        sys.exit(stats["error"])

    in_use = set(from_values)
    if args.skip_cluster:
        print("cluster:      skipped (--skip-cluster)")
    else:
        from_cluster = in_use_from_cluster(args.registry)
        if from_cluster is None:
            print("cluster:      WARNING unreachable, using values files only")
            stats["warnings"].append(
                "cluster unreachable; safe-list came from values files only"
            )
        else:
            extra = from_cluster - from_values
            print(
                "cluster:      %d image refs (%d not in values files)"
                % (len(from_cluster), len(extra))
            )
            for repo, tag in sorted(extra):
                print("  only on cluster: %s:%s" % (repo, tag))
            in_use |= from_cluster

    print("\nsafe-list (%d):" % len(in_use))
    for repo, tag in sorted(in_use):
        print("  %s:%s" % (repo, tag))
    stats["safe_list"] = in_use

    # --- run the phases -----------------------------------------------------
    # Any failure from here on still has to produce a summary, otherwise the
    # run that most needs explaining is the one that explains nothing.
    try:
        stats["unlocked"] = phase_unlock(args.registry, args.dry_run)
        stats["locked"] = phase_lock(args.registry, in_use, args.dry_run)
        if args.lock_only:
            print("\n--lock-only: stopping before purge.")
            stats["after"] = show_usage(args.registry, "\nafter")
            return
        stats["counts"] = phase_purge(args.registry, rules, args.dry_run)
    except RuntimeError as exc:
        stats["error"] = str(exc)
        raise
    finally:
        if stats.get("error") is None:
            stats["after"] = show_usage(args.registry, "\nafter")
        write_summary(args.summary_md, stats)

    print("\ndone%s" % (" (dry run, nothing changed)" if args.dry_run else ""))


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        sys.exit("ERROR: %s" % exc)
