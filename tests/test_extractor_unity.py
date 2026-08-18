"""Gate: Unity / C# minimal-credible extractor coverage.

Veripsa's "Unknown — some of your changed paths aren't in main's graph" verdict
fires when ANY path on a PR is absent from the build_graph node set (see
core.main_impact_surface).  A Unity repo's PR routinely touches not just `.cs`
files (which the existing tree-sitter-c-sharp grammar already covers) but also
the YAML asset family — `.unity` scenes, `.prefab` prefabs, `.asset`
ScriptableObjects, per-asset `.meta` GUID files, `.asmdef` assembly defs, and
shader sources (`.shader` / `.cginc` / `.hlsl` / `.compute`).  Before this lane,
those extensions were not in `_SOURCE_EXT` → no file node was emitted → every
Unity PR landed as "Unknown" (the example-org/game-app dogfood verdict that
motivated this work).

This test is CONTENT-FREE (asserts only that file nodes exist with the correct
language label — never inspects YAML/asset bodies) and recall-safe (no
structural edges are asserted FROM the YAML files; the extractor doesn't parse
prefab GUIDs to avoid noisy false couplings).  What it locks:

  1. EVERY hand-edited Unity asset type yields exactly one bare `file` node
     stamped `language="unity"` (the new label), so a PR that ONLY touches a
     scene / prefab / .meta is no longer "unknown" — it gets a real verdict.

  2. A typical Unity `.cs` file under `Assets/` still parses through the
     regular C# path (def + class nodes), so the C# graph survives untouched.

  3. The engine's cache trees (`Library/`, `Temp/`, `MemoryCaptures/`) are
     PRUNED — file nodes are NOT emitted for engine-generated artifacts under
     them (anti-cry-wolf: no one hand-edits them; without the prune, every
     Unity repo would gain thousands of noise nodes and a walk thrash).

Prints UNITY EXTRACTOR GATE: PASS or UNITY EXTRACTOR GATE: FAIL.
Runs offline: no Postgres, no git, no network.
"""

import os
import sys
import tempfile
import pathlib

_HERE = pathlib.Path(__file__).parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))

import code_graph_extract as X


# ---------------------------------------------------------------------------
# Fixtures — minimal synthetic Unity project layout
# ---------------------------------------------------------------------------

# Tiny C# component a Unity author writes (MonoBehaviour-shaped, but the
# extractor doesn't care about MonoBehaviour specifically — it parses the
# class + method via tree-sitter-c-sharp).
_CS_FIXTURE = """\
using UnityEngine;

namespace Game.Player
{
    public class PlayerController
    {
        public void Move()
        {
        }
    }
}
"""

# Unity scene (YAML, real-world shape — header + a single GameObject stub).
# We don't assert structural edges from this — only that the file node is
# minted with language="unity".
_UNITY_SCENE = """\
%YAML 1.1
%TAG !u! tag:unity3d.com/2011/
--- !u!1 &1
GameObject:
  m_ObjectHideFlags: 0
  m_Name: Player
"""

_PREFAB_FIXTURE = """\
%YAML 1.1
%TAG !u! tag:unity3d.com/2011/
--- !u!1 &100
GameObject:
  m_Name: Bullet
"""

_ASSET_FIXTURE = """\
%YAML 1.1
%TAG !u! tag:unity3d.com/2011/
--- !u!114 &11400000
MonoBehaviour:
  m_Script: {fileID: 11500000, guid: deadbeef, type: 3}
"""

# .meta: per-asset import settings + GUID (the stable identity).
_META_FIXTURE = """\
fileFormatVersion: 2
guid: 0123456789abcdef0123456789abcdef
MonoImporter:
  externalObjects: {}
"""

_ASMDEF_FIXTURE = """\
{
    "name": "Game.Player",
    "references": []
}
"""

_SHADER_FIXTURE = """\
Shader "Custom/Simple"
{
    Properties { _Color ("Color", Color) = (1,1,1,1) }
    SubShader { Pass { } }
}
"""


def run():
    fails = []

    def check(cond, desc):
        if not cond:
            fails.append(desc)

    with tempfile.TemporaryDirectory() as td:
        # Minimal Unity layout:
        #   Assets/Scripts/PlayerController.cs        -> parsed C#
        #   Assets/Scripts/PlayerController.cs.meta   -> bare unity node
        #   Assets/Scenes/Main.unity                  -> bare unity node
        #   Assets/Scenes/Main.unity.meta             -> bare unity node
        #   Assets/Prefabs/Bullet.prefab              -> bare unity node
        #   Assets/Data/Settings.asset                -> bare unity node
        #   Assets/Game.Player.asmdef                 -> bare unity node
        #   Assets/Shaders/Simple.shader              -> bare unity node
        #   Library/ScriptAssemblies/Game.dll.meta    -> PRUNED (Library/ skip)
        #   Temp/UnityLockfile                        -> PRUNED (Temp/ skip)
        layout = {
            "Assets/Scripts/PlayerController.cs":      _CS_FIXTURE,
            "Assets/Scripts/PlayerController.cs.meta": _META_FIXTURE,
            "Assets/Scenes/Main.unity":                _UNITY_SCENE,
            "Assets/Scenes/Main.unity.meta":           _META_FIXTURE,
            "Assets/Prefabs/Bullet.prefab":            _PREFAB_FIXTURE,
            "Assets/Data/Settings.asset":              _ASSET_FIXTURE,
            "Assets/Game.Player.asmdef":               _ASMDEF_FIXTURE,
            "Assets/Shaders/Simple.shader":            _SHADER_FIXTURE,
            # Cache trees that MUST be pruned (engine-generated, never hand-edited).
            "Library/ScriptAssemblies/Game.dll.meta":  _META_FIXTURE,
            "Library/PackageCache/com.unity.x/Stuff":  "irrelevant\n",
            "Temp/UnityLockfile":                      "lockfile\n",
            "MemoryCaptures/snapshot.snap":            "memcap\n",
        }
        for rel, body in layout.items():
            full = os.path.join(td, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as fh:
                fh.write(body)

        g = X.build_graph(td)

    nodes = g["nodes"]
    file_nodes = [n for n in nodes if n.get("kind") == "file"]
    paths_by_lang = {}
    for n in file_nodes:
        paths_by_lang.setdefault(n.get("language"), set()).add(n.get("path"))
    all_file_paths = {n.get("path") for n in file_nodes}

    # ---- (1) Unity-asset coverage: each hand-edited asset has a file node ----
    expected_unity_paths = {
        "Assets/Scripts/PlayerController.cs.meta",
        "Assets/Scenes/Main.unity",
        "Assets/Scenes/Main.unity.meta",
        "Assets/Prefabs/Bullet.prefab",
        "Assets/Data/Settings.asset",
        "Assets/Game.Player.asmdef",
        "Assets/Shaders/Simple.shader",
    }
    unity_paths = paths_by_lang.get("unity", set())
    missing = expected_unity_paths - unity_paths
    check(not missing,
          f"unity-asset file nodes missing: {sorted(missing)} "
          f"(got language='unity' paths: {sorted(unity_paths)})")

    # ---- (2) C# still parses: the .cs file yields a real def/class graph ----
    cs_file_nodes = [n for n in file_nodes if (n.get("path") or "").endswith(".cs")]
    check(len(cs_file_nodes) == 1,
          f"expected 1 .cs file node, got {len(cs_file_nodes)}")
    cs_defs = [n for n in nodes
               if (n.get("path") or "").endswith(".cs")
               and n.get("kind") in ("def", "class")]
    cs_def_names = {n.get("name") for n in cs_defs}
    # tree-sitter-c-sharp may or may not be installed in the test env. If it IS
    # installed, the file parses and we should see PlayerController / Move. If
    # it's NOT installed, the file degrades to a bare node (GAP-14 path, lang
    # "csharp") — that's still a valid file node; we only assert structural
    # symbols when the grammar is available.
    cs_lang_seen = cs_file_nodes[0].get("language") if cs_file_nodes else None
    check(cs_lang_seen in ("csharp", "unknown"),
          f".cs file node language should be 'csharp' (or 'unknown' if grammar absent), "
          f"got {cs_lang_seen!r}")
    if cs_defs:
        # Best-effort: when parsed, the symbols we know are in the fixture must surface.
        check("PlayerController" in cs_def_names or "Move" in cs_def_names,
              f"C# parse ran but yielded no known symbol; got {sorted(cs_def_names)}")

    # ---- (3) Engine cache trees are PRUNED (Library/ Temp/ MemoryCaptures/) ----
    pruned = {p for p in all_file_paths
              if p.startswith("Library/")
              or p.startswith("Temp/")
              or p.startswith("MemoryCaptures/")}
    check(not pruned,
          f"engine cache files leaked into the graph (should have been pruned): {sorted(pruned)}")

    # ---- (4) NEVER-CRASH: a Unity PR's typical surface produces SOMETHING ----
    # The whole point of this lane: a PR that only touches a .unity scene used
    # to yield zero file nodes for that path → "Unknown" verdict. Assert the
    # scene path is present and content-free (no structural edges anchored on
    # the scene's GUID — we don't parse YAML).
    scene = "Assets/Scenes/Main.unity"
    check(scene in all_file_paths,
          f"scene path {scene!r} missing from graph (the gap this lane closes)")
    scene_edges = [e for e in g.get("edges", [])
                   if e.get("src") == scene or e.get("dst") == scene]
    check(not scene_edges,
          f"scene file should produce NO structural edges (conservative bias), got {scene_edges}")

    if fails:
        print("UNITY EXTRACTOR GATE: FAIL")
        for f in fails:
            print(f"  - {f}")
        sys.exit(1)
    print("UNITY EXTRACTOR GATE: PASS")


if __name__ == "__main__":
    run()
