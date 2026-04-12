import pkg_resources
from packaging.requirements import Requirement
from packaging.version import Version, InvalidVersion

# 已安装的包
installed = {pkg.key for pkg in pkg_resources.working_set}

def read_requirements(path):
    """读取 requirements 文件，返回 {包名: Requirement对象}"""
    reqs = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                r = Requirement(line)
                reqs[r.name.lower()] = r
            except Exception as e:
                print(f"⚠️ 解析失败: {line}, 错误: {e}")
    return reqs

# 读取两份 requirements
reqs1 = read_requirements("requirements_lc.txt")
reqs2 = read_requirements("requirements_uied.txt")

# 合并逻辑：选择更“严格/更高版本”的约束
merged = {}

for pkg in set(reqs1.keys()).union(reqs2.keys()):
    r1 = reqs1.get(pkg)
    r2 = reqs2.get(pkg)

    if r1 and r2:
        # 取交集 → 如果交集为空，就取更高版本的下限
        specs = list(r1.specifier) + list(r2.specifier)
        # 先尝试合并约束
        if specs:
            # 找到最大的下限版本
            min_version = None
            for s in specs:
                if s.operator in (">=", "==", "~="):
                    try:
                        v = Version(s.version)
                        if min_version is None or v > min_version:
                            min_version = v
                    except InvalidVersion:
                        pass
            if min_version:
                merged[pkg] = f"{pkg}>={min_version}"
            else:
                merged[pkg] = str(r1)  # fallback
        else:
            merged[pkg] = str(r1)
    else:
        merged[pkg] = str(r1 or r2)

# 过滤掉已安装的包
to_install = {}
for pkg, spec in merged.items():
    if pkg in installed:
        continue
    to_install[pkg] = spec

# 写回 requirements.txt
with open("requirements.txt", "w") as f:
    for pkg, spec in sorted(to_install.items()):
        f.write(spec + "\n")

print("✅ 合并并过滤完成，已写入 requirements.txt")
