#!/usr/bin/env python3

import argparse, os, asyncio
from collections import namedtuple
from package_tree import Package
import pacman, utils

parser = argparse.ArgumentParser(description="AUR package management made easy")
primary = parser.add_mutually_exclusive_group()
primary.add_argument("-S", action='store_true', default=False, dest='install', help="Install package(s) from AUR")
primary.add_argument("-Ss", action='store_true', default=False, dest='search', help="Search for package(s) in AUR")
primary.add_argument("-Si", action='store_true', default=False, dest='info', help="Get detailed info on packages in AUR")
primary.add_argument("-Syu", action='store_true', default=False, dest='upgrade', help="Upgrade all out-of-date AUR-packages")
primary.add_argument("-Sc", action='store_true', default=False, dest='clean', help="Clean cache of all uninstalled package files")
primary.add_argument("-Scc", action='store_true', default=False, dest='fullclean', help="Clean cache of all package files, including installed")
parser.add_argument("--asdeps", action='store_true', default=False, dest='asdeps', help="If packages are installed, install them as dependencies")
parser.add_argument("--local-path", action='store', default='~/.blinky', dest='aur_local', help="Local path for building and cache")
parser.add_argument("--keep-sources", action='store', default='none', dest='keep_sources', help="Keep sources, can be 'none', 'skipped', for keeping skipped packages only, or 'all'")
parser.add_argument("--build-only", action='store_true', default=False, dest='buildonly', help="Only build, do not install anything")
parser.add_argument("pkg_candidates", metavar="pkgname", type=str, nargs="*", help="packages to install/build")
parser.add_argument('--verbose', '-v', action='count', default=0, dest='verbosity')

args = parser.parse_args()

Config = namedtuple('Context', ['cachedir', 'builddir', 'logdir', 'v'])

# process arguments if necessary
args.aur_local = os.path.abspath(os.path.expanduser(args.aur_local))

ctx = Config(
		cachedir=os.path.join(args.aur_local, 'cache'),
		builddir=os.path.join(args.aur_local, 'build'),
		logdir=os.path.join(args.aur_local, 'logs'),
		v=args.verbosity
		)
os.makedirs(ctx.cachedir, exist_ok=True)
os.makedirs(ctx.builddir, exist_ok=True)
os.makedirs(ctx.logdir, exist_ok=True)
utils.logmsg(ctx.v, 2, ("builddir: {}".format(ctx.builddir)))
utils.logmsg(ctx.v, 2, ("cachedir: {}".format(ctx.cachedir)))
utils.logmsg(ctx.v, 2, ("makepkg-logdir: {}".format(ctx.logdir)))

if args.buildonly:
	utils.logmsg(ctx.v, 0, "Sources can be found at {}".format(ctx.builddir))


def build_packages_from_aur(package_candidates, install_as_dep=False):
	aurpkgs, repopkgs, notfoundpkgs = utils.check_in_aur(package_candidates)

	if repopkgs:
		utils.logmsg(ctx.v, 1, "Skipping: {}: packaged in repos".format(", ".join(repopkgs)))
	if notfoundpkgs:
		utils.logmsg(ctx.v, 1, "Skipping: {}: neither in repos nor AUR".format(", ".join(notfoundpkgs)))

	packages = []
	skipped_packages = []
	utils.logmsg(ctx.v, 0, "Fetching information and files for dependency-graph for {} package{}".format(len(aurpkgs), '' if len(aurpkgs) == 1 else 's'))


	async def gen_package_obj(pkgnamelist, ctx):
		pkgobj = []
		loop = asyncio.get_event_loop()
		futures = [
			loop.run_in_executor(
				None,
				Package,
				p, ctx
			)
			for p in pkgnamelist
		]
		for p in await asyncio.gather(*futures):
			pkgobj.append(p)

		return pkgobj

	loop = asyncio.get_event_loop()
	packages = loop.run_until_complete(gen_package_obj(aurpkgs, ctx))


	for p in packages:
		if not p.review():
			utils.logmsg(ctx.v, 0, "Skipping: {}: Did not pass review".format(p.name))
			skipped_packages.append(p)

	# drop all packages that did not pass review
	for p in skipped_packages:
		packages.remove(p)

	uninstalled_makedeps = set()
	skipped_due_to_missing_makedeps = []
	for p in packages:
		md = p.get_makedeps()
		md_not_found = [p for p in md if not p.installed and not p.in_repos and not p.in_aur]
		if len(md_not_found) > 0:
			utils.logerr(None, "{}: cannot satisfy makedeps from either repos, AUR or local installed packages, skipping".format(p.name))
			skipped_packages.append(p)
			skipped_due_to_missing_makedeps.append(p)

		md_available = set([p for p in md if not p.installed and (p.in_repos or p.in_aur)])

		uninstalled_makedeps = uninstalled_makedeps.union(md_available)

	# drop all packages whose makedeps cannot be satisfied
	for p in skipped_due_to_missing_makedeps:
		packages.remove(p)

	md_aur = [p for p in uninstalled_makedeps if p.in_aur]
	if len(md_aur) > 0:
		utils.logmsg(ctx.v, 0, "Building makedeps from aur: {}".format(", ".join(p.name for p in md_aur)))
		build_packages_from_aur(md_aur, install_as_dep=True)

	repodeps = set()
	for p in packages:
		repodeps = repodeps.union(p.get_repodeps())

	md_repos = [p.name for p in uninstalled_makedeps if p.in_repos]
	repodeps_uninstalled = [p.name for p in repodeps if not p.installed]
	to_be_installed = set(repodeps_uninstalled).union(md_repos)

	if to_be_installed:
		utils.logmsg(ctx.v, 0, "Installing dependencies and makedeps from repos")
		if not pacman.install_repo_packages(to_be_installed, asdeps=True):
			utils.logerr(0, "Could not install deps and makedeps from repos")

	for p in packages:
		success = p.build(buildflags=['-Cfd'], recursive=True)
		if success:
			od = p.get_optdeps()
			for name, optdeplist in od:
				print(" :: Package {} has optional dependencies:")
				for odname in optdeplist:
					print("     - {}".format(odname))

	built_pkgs = set()
	built_deps = set()
	for p in packages:
		built_pkgs = built_pkgs.union(set(p.built_pkgs))
		for d in p.deps:
			built_deps = built_deps.union(d.get_built_pkgs())

	os.chdir(ctx.cachedir)

	if args.buildonly:
		utils.logmsg(ctx.v, 1, "Packages have been built:")
		utils.logmsg(ctx.v, 1, ", ",join(built_deps + built_pkgs) or "None")
	else:
		if built_deps:
			utils.logmsg(ctx.v, 0, "Installing package dependencies")
			if not pacman.install_package_files(built_deps, asdeps=True):
				utils.logerr(2, "Failed to install built package dependencies")

		if built_pkgs:
			utils.logmsg(ctx.v, 0, "Installing built packages")
			if not pacman.install_package_files(built_pkgs, asdeps=install_as_dep):
				utils.logerr(2, "Failed to install built packages")
		else:
			utils.logmsg(ctx.v, 0, "No packages built, nothing to install")

	if uninstalled_makedeps:
		utils.logmsg(ctx.v, 0, "Removing previously uninstalled makedeps")
		if not pacman.remove_packages([p for p in uninstalled_makedeps if pacman.is_installed(p.name)]):
			utils.logerr(None, "Failed to remove previously uninstalled makedeps")

	if not args.keep_sources == "all":
		for p in packages:
			p.remove_sources()

	if not args.keep_sources in ["all", "skipped"]:
		for p in skipped_packages:
			p.remove_sources()


def clean_cache(keep_installed=False):
	pkgs = os.listdir(ctx.cachedir)
	files_to_remove = []
	for p in pkgs:
		*pkgnameparts, pkgver, pkgrel, pkgarch = p.split(".pkg.")[0].split("-")
		pkgname = "-".join(pkgnameparts)

		pkgfiles = [pkg for pkg in pkgs if pkg.startswith(pkgname)]

		if pacman.is_installed(pkgname) and keep_installed:
			files_to_remove += pkgfiles[:-1]
		else:
			files_to_remove += pkgfiles

	os.chdir(ctx.cachedir)
	for f in files_to_remove:
		os.remove(f)




if __name__ == "__main__":
	if args.install:
		build_packages_from_aur(args.pkg_candidates, install_as_dep=args.asdeps)
	if args.search:
		aurdata = utils.query_aur("search", args.pkg_candidates)
		if aurdata["resultcount"] == 0:
			utils.logmsg(ctx.v, 0, "No results found")
		else:
			for pkgdata in aurdata["results"]:
				print("aur/{} {}".format(pkgdata["Name"], pkgdata["Version"]))
				print("    " + pkgdata["Description"])
	if args.info:
		from templates import pkginfo
		foundSth = False
		for pkg in args.pkg_candidates:
			pkgdata = utils.query_aur("info", pkg, single=True)
			if pkgdata:
				foundSth = True
				print(pkginfo.format(
						name=pkgdata.get("Name"),
						version=pkgdata.get("Version"),
						desc=pkgdata.get("Description"),
						url=pkgdata.get("URL"),
						license=", ".join(pkgdata.get("License") or ["None"]),
						groups=", ".join(pkgdata.get("Groups") or ["None"]),
						provides=", ".join(pkgdata.get("Provides") or ["None"]),
						deps=", ".join(pkgdata.get("Depends") or ["None"]),
						optdeps=", ".join(pkgdata.get("OptDepends") or ["None"]),
						makedeps=", ".join(pkgdata.get("MakeDepends") or ["None"]),
						conflicts=", ".join(pkgdata.get("Conflicts") or ["None"]),
						replaces=", ".join(pkgdata.get("Replaces") or ["None"]),
						maintainer=pkgdata.get("Maintainer"),
						submitted=pkgdata.get("FirstSubmitted"),
						numvotes=pkgdata.get("NumVotes"),
						popularity=pkgdata.get("Popularity"),
						outofdate=pkgdata.get("OutOfDate") or "No"
						))

		if not foundSth:
			utils.logmsg(ctx.v, 0, "No results found")

	if args.upgrade:
		foreign_pkg_v = pacman.get_foreign_package_versions()
		aurdata = utils.query_aur("info", foreign_pkg_v.keys())
		upgradable_pkgs = []
		for pkgdata in aurdata["results"]:
			if pkgdata["Name"] in foreign_pkg_v:
				if pkgdata["Version"] > foreign_pkg_v[pkgdata["Name"]]:
					upgradable_pkgs.append(pkgdata["Name"])

		build_packages_from_aur(upgradable_pkgs, install_as_dep=args.asdeps)

	if args.clean:
		clean_cache(keep_installed=True)
	if args.fullclean:
		clean_cache(keep_installed=False)
