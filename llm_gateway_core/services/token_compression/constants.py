"""RTK Token Compression constants."""

RAW_CAP = 10 * 1024 * 1024  # 10 MiB
MIN_COMPRESS_SIZE = 500       # bytes; skip tiny blobs
DETECT_WINDOW = 1024          # autodetect peeks first N chars

GIT_DIFF_HUNK_MAX_LINES = 100
DEDUP_LINE_MAX = 2000

GREP_PER_FILE_MAX = 10
FIND_PER_DIR_MAX = 10
FIND_TOTAL_DIR_MAX = 20

# git status caps
STATUS_MAX_FILES = 10
STATUS_MAX_UNTRACKED = 10

# ls compact_ls
LS_EXT_SUMMARY_TOP = 5
LS_NOISE_DIRS = frozenset({
    "node_modules", ".git", "target", "__pycache__",
    ".next", "dist", "build", ".venv", "venv",
    ".cache", ".idea", ".vscode", ".DS_Store",
})

# tree
TREE_MAX_LINES = 200

# Cursor Glob search list
SEARCH_LIST_PER_DIR_MAX = 10
SEARCH_LIST_TOTAL_DIR_MAX = 20

# Smart truncate
SMART_TRUNCATE_HEAD = 120
SMART_TRUNCATE_TAIL = 60
SMART_TRUNCATE_MIN_LINES = 250

# readNumbered
READ_NUMBERED_MIN_HIT_RATIO = 0.7

# Filter name strings
FILTER_GIT_DIFF = "git-diff"
FILTER_GIT_STATUS = "git-status"
FILTER_GIT_LOG = "git-log"
FILTER_GREP = "grep"
FILTER_FIND = "find"
FILTER_LS = "ls"
FILTER_TREE = "tree"
FILTER_DEDUP_LOG = "dedup-log"
FILTER_SMART_TRUNCATE = "smart-truncate"
FILTER_READ_NUMBERED = "read-numbered"
FILTER_SEARCH_LIST = "search-list"
FILTER_BUILD_OUTPUT = "build-output"
