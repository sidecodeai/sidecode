"""The prompt Codex's own `/init` sends, so a remote `/init` writes the same file.

Read out of the Codex 0.154.0 binary rather than written here: the terminal
turns `/init` into this text and nothing else, and a device that paraphrased it
would produce a different AGENTS.md from the one the same command writes in the
terminal beside it (amendment A27).
"""

from __future__ import annotations

INIT_PROMPT = (
    "Generate a file named AGENTS.md that serves as a contributor guide for this"
    " repository.\n"
    "Before writing, check whether AGENTS.md already exists in the current working"
    " directory. If it does, do not overwrite or modify it.\n"
    "Your goal is to produce a clear, concise, and well-structured document with"
    " descriptive headings and actionable explanations for each section.\n"
    "Follow the outline below, but adapt as needed — add sections if relevant, and omit"
    " those that do not apply to this project.\n"
    "\n"
    "Document Requirements\n"
    "\n"
    '- Title the document "Repository Guidelines".\n'
    "- Use Markdown headings (#, ##, etc.) for structure.\n"
    "- Keep the document concise. 200-400 words is optimal.\n"
    "- Keep explanations short, direct, and specific to this repository.\n"
    "- Provide examples where helpful (commands, directory paths, naming patterns).\n"
    "- Maintain a professional, instructional tone.\n"
    "\n"
    "Recommended Sections\n"
    "\n"
    "Project Structure & Module Organization\n"
    "\n"
    "- Outline the project structure, including where the source code, tests, and assets"
    " are located.\n"
    "\n"
    "Build, Test, and Development Commands\n"
    "\n"
    "- List key commands for building, testing, and running locally (e.g., npm test, make"
    " build).\n"
    "- Briefly explain what each command does.\n"
    "\n"
    "Coding Style & Naming Conventions\n"
    "\n"
    "- Specify indentation rules, language-specific style preferences, and naming patterns.\n"
    "- Include any formatting or linting tools used.\n"
    "\n"
    "Testing Guidelines\n"
    "\n"
    "- Identify testing frameworks and coverage requirements.\n"
    "- State test naming conventions and how to run tests.\n"
    "\n"
    "Commit & Pull Request Guidelines\n"
    "\n"
    # Codex writes a typographic apostrophe here; the prompt is quoted verbatim.
    "- Summarize commit message conventions found in the project’s Git history.\n"  # noqa: RUF001
    "- Outline pull request requirements (descriptions, linked issues, screenshots, etc.).\n"
    "\n"
    "(Optional) Add other sections if relevant, such as Security & Configuration Tips,"
    " Architecture Overview, or Agent-Specific Instructions.\n"
    ""
)
