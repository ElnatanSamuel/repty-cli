from typing import List, Dict, Any, Optional
import json
import re

from . import db, config


_STOPWORDS = {
    "a", "an", "the", "of", "for", "to", "in", "on", "with", "and", "or",
    "command", "commands", "please", "find", "show", "me", "file", "files",
}


def _clean_query(raw: str) -> str:
    terms = re.findall(r"\w+", (raw or "").lower())
    base: list[str] = []
    for t in terms:
        if len(t) < 3 or t in _STOPWORDS:
            continue
        # simple stemming: -ing, plural -s
        variants = {t}
        if len(t) >= 5 and t.endswith("ing"):
            stem = t[:-3]
            variants.add(stem)
            variants.add(stem + "e")
        if len(t) >= 4 and t.endswith("s"):
            variants.add(t[:-1])
        base.extend(sorted(variants))
    # dedupe while preserving order
    seen = set()
    out = []
    for w in base:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return " ".join(out)


def ai_search(query: str, limit: int = 50, context_limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Return ranked results with explanations using Gemini.

    Output: list of dicts: {id, score, reason, row}
    """
    try:
        cfg = config.load_config()
        api_key = config.require_api_key(cfg)
        model_name = cfg.get("ai_model", "gemini-1.5-flash")
        ctx = context_limit or int(cfg.get("ai_context_limit", 500))

        import google.generativeai as genai

        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(model_name)

        with db.connect() as conn:
            # Prefer a targeted candidate set from local FTS to reduce noise
            cleaned = _clean_query(query)
            # Try strict AND first
            match_q = db.build_fts_query(cleaned or query, mode="and", prefix=True)
            rows = db.search_fts(conn, match_q or cleaned or query, limit=min(limit * 3, 50))
            if not rows:
                # Relax to OR if AND finds nothing
                match_q_or = db.build_fts_query(cleaned or query, mode="or", prefix=True)
                rows = db.search_fts(conn, match_q_or or cleaned or query, limit=min(limit * 3, 50))
            # If FTS yields nothing even with OR, do not fall back to recency candidates.
            # Return no candidates so AI results are empty rather than noisy.

            # Include tag-based matches along with FTS candidates
            tokens = set((cleaned or "").split())
            try:
                tag_rows = db.search_by_tags(conn, list(tokens), limit=min(limit * 2, 50)) if tokens else []
            except Exception:
                tag_rows = []
            if tag_rows:
                by_id = {r["id"]: r for r in rows}
                for tr in tag_rows:
                    if tr["id"] not in by_id:
                        by_id[tr["id"]] = tr
                rows = list(by_id.values())

            # Re-rank local candidates: tag similarity first, then favorites
            tokens = set((cleaned or "").split())
            def tag_rank(r) -> tuple:
                tagstr = (r["tags"] or "").lower()
                parts = []
                for p in tagstr.split(","):
                    p = p.strip()
                    if not p:
                        continue
                    parts.append(p)
                    if p.startswith("desc:"):
                        parts.append(p[5:])
                has_match = False
                if tokens and parts:
                    for t in tokens:
                        for tp in parts:
                            if t in tp or tp in t:
                                has_match = True
                                break
                        if has_match:
                            break
                fav = any(tp == "favorite" for tp in parts)
                return (1 if has_match else 0, 1 if fav else 0)
            rows = sorted(rows, key=tag_rank, reverse=True)

            # Prepare concise context and exclude internal commands
            commands_ctx = []
            filtered_rows = []
            for r in rows:
                cmd = (r["command"] or "").strip()
                if cmd.startswith("repty ") or cmd.startswith(". ") or cmd.startswith("source "):
                    continue
                commands_ctx.append({
                    "id": r["id"],
                    "command": r["command"],
                    "cwd": r["cwd"],
                    "timestamp": r["timestamp"],
                    "exit_code": r["exit_code"],
                    "tags": (r["tags"] or "").split(",") if r["tags"] else [],
                })
                filtered_rows.append(r)
            # Use the filtered rows for ID mapping
            rows = filtered_rows

            # Hard relevance filter: if we have action tokens from the query, restrict
            # candidates to those whose command contains at least one token. If none match,
            # keep the original set to avoid zero-candidate situations.
            tokens_list = (cleaned or "").split()
            # Derive action tokens by removing generic tooling nouns that cause false positives
            _GENERIC = {
                "git", "bash", "zsh", "fish", "python", "pip", "pipx", "node", "npm", "pnpm",
                "yarn", "docker", "kubectl", "kube", "ssh", "curl", "wget",
            }
            action_tokens = [t for t in tokens_list if t not in _GENERIC and len(t) >= 4]
            if action_tokens:
                rows_cmd_filtered = [
                    r for r in rows
                    if any(t in ((r["command"] or "").lower()) for t in action_tokens)
                ]
                if rows_cmd_filtered:
                    rows = rows_cmd_filtered
                    # Also narrow commands_ctx to the same ids
                    allowed_ids = {r["id"] for r in rows}
                    commands_ctx = [c for c in commands_ctx if c["id"] in allowed_ids]

        prompt = {
            "query": query,
            "instructions": (
                "You are a CLI command retrieval assistant. Given the user's natural language query and a list of previously executed commands, "
                "identify the most relevant commands. Prefer commands that likely succeeded (exit_code==0) and whose cwd or tags match the intent. "
                "You MUST choose only from the provided 'commands' list (these are candidates from local FTS); do NOT invent ids. "
                "If 'tokens' are provided, ONLY select commands whose command string contains at least one of those tokens (case-insensitive). "
                "Favor favorites only when relevant to the query. "
                "Return a JSON array of objects with fields: id (int), score (float 0-10 where 10 is best), reason (short string). "
                "Output MUST be a JSON array only. No prose, no prefixes/suffixes, and NO code fences."
            ),
            "commands": commands_ctx,
            # Only provide action tokens to avoid matching on generic words like 'git'
            "tokens": action_tokens,
        }

        # Request pure JSON back
        # Send a single text prompt containing JSON; request JSON back
        response = model.generate_content(
            json.dumps(prompt),
            generation_config={
                "response_mime_type": "application/json",
            },
        )

        text = response.text or "[]"

        def _strip_code_fences(s: str) -> str:
            # Remove ```json ... ``` or ``` ... ``` wrappers if present
            return re.sub(r"```(?:json)?\s*([\s\S]*?)\s*```", r"\1", s, flags=re.IGNORECASE)

        def _parse_json_array(s: str):
            s1 = _strip_code_fences(s).strip()
            # First try whole string
            try:
                parsed = json.loads(s1)
                if isinstance(parsed, list):
                    return parsed
            except Exception:
                pass
            # Try to find first JSON array substring
            try:
                candidates = re.findall(r"\[[\s\S]*\]", s1)
                for c in candidates:
                    try:
                        parsed = json.loads(c)
                        if isinstance(parsed, list):
                            return parsed
                    except Exception:
                        continue
            except Exception:
                pass
            return []

        data = _parse_json_array(text)

        # Map back to the SAME context rows we provided to the model
        id_to_row = {r["id"]: r for r in rows}

        results: List[Dict[str, Any]] = []
        for item in data:
            try:
                cid = int(item.get("id"))
                score = float(item.get("score", 0.0))
                reason = str(item.get("reason", ""))
                r = id_to_row.get(cid)
                if r is None:
                    continue
                # Skip internal commands (but allow legitimate shell like 'source')
                cmd = (r["command"] or "").strip()
                if cmd.startswith("repty ") or cmd.startswith(". ") or cmd.startswith("source "):
                    continue
                results.append({
                    "id": cid,
                    "score": score,
                    "reason": reason,
                    "row": r,
                })
            except Exception:
                continue

        # Apply score threshold (configurable), then deduplicate by command string.
        try:
            threshold = float(cfg.get("ai_min_score", 3.0))
        except Exception:
            threshold = 3.0
        results = [it for it in results if it.get("score", 0.0) >= threshold]

        # Deduplicate by command string; keep highest score; tie-break by newer id
        best_by_cmd: Dict[str, Dict[str, Any]] = {}
        for it in results:
            cmd = it["row"]["command"]
            prev = best_by_cmd.get(cmd)
            if prev is None:
                best_by_cmd[cmd] = it
                continue
            if it["score"] > prev["score"] or (it["score"] == prev["score"] and it["id"] > prev["id"]):
                best_by_cmd[cmd] = it

        deduped = list(best_by_cmd.values())
        # Sort by score descending, then newer id
        deduped.sort(key=lambda x: (-x["score"], -x["id"]))
        out = deduped[:limit]
        return out

    except Exception as e:
        # Bubble up as a signal to fallback
        raise RuntimeError(f"Gemini AI search failed: {e}")
