import requests, subprocess, os, shutil, sys, stat
import pacman, utils

# pkg_store holds all packages so that we have all package-objects
# to build the fully interconnected package graph
pkg_store    = {}
srcpkg_store = {}


def parse_dep_pkg(pkgname, ctx, parentpkg=None):
	packagename = pkgname.split('>=')[0].split('=')[0]

	if packagename not in pkg_store:
		pkg_store[packagename] = Package(packagename, ctx=ctx, firstparent=parentpkg)
	elif parentpkg:
		pkg_store[packagename].parents.append(parentpkg)

	return pkg_store[packagename]


def parse_src_pkg(src_id, version, tarballpath, ctx):
	if src_id not in srcpkg_store:
		srcpkg_store[src_id] = SourcePkg(src_id, version, tarballpath, ctx=ctx)

	return srcpkg_store[src_id]


def pkg_in_cache(pkg):
	pkgs = []
	pkgprefix = '{}-{}-'.format(pkg.name, pkg.version_latest)
	for pkg in os.listdir(pkg.ctx.cachedir):
		if pkgprefix in pkg:
			# was already built at some point
			pkgs.append(pkg)
	return pkgs


class SourcePkg:

	def __init__(self, name, version, tarballpath, ctx=None):
		self.ctx           = ctx
		self.name          = name
		self.version       = version
		self.tarballpath   = 'https://aur.archlinux.org' + tarballpath
		self.tarballname   = tarballpath.split('/')[-1]
		self.reviewed      = False
		self.review_passed = False
		self.built         = False
		self.build_success = False
		self.srcdir        = None

	def download(self):
		os.chdir(self.ctx.builddir)
		r = requests.get(self.tarballpath)
		with open(self.tarballname, 'wb') as tarball:
			tarball.write(r.content)

	def extract(self):
		subprocess.call(['tar', '-xzf', self.tarballname])
		os.remove(self.tarballname)
		self.srcdir = os.path.join(self.ctx.builddir, self.name)


	def build(self, buildflags=[]):
		if self.built:
			return self.build_success

		os.chdir(self.srcdir)
		self.built = True

		# prepare logfiles
		stdoutlogfile = os.path.join(self.ctx.logdir, "{}-{}.stdout.log".format(self.name, self.version))
		stderrlogfile = os.path.join(self.ctx.logdir, "{}-{}.stderr.log".format(self.name, self.version))

		with open(stdoutlogfile, 'w') as outlog, open(stderrlogfile, 'w') as errlog:
			p = subprocess.Popen(['makepkg'] + buildflags, stdout=outlog, stderr=errlog)
			r = p.wait()

		if r != 0:
			with open(stdoutlogfile, 'a') as outlog, open(stderrlogfile, 'a') as errlog:
				print("\nexit code: {}".format(self.name, r), file=outlog)
				print("\nexit code: {}".format(self.name, r), file=errlog)
			self.build_success = False
			return False
		else:
			self.build_success = True
			return True

	def set_review_state(self, state):
		"""This function is a helper to keep self.review clean and readable"""
		self.review_passed = state
		self.reviewed = True
		return self.review_passed

	def review(self):
		if self.reviewed:
			return self.review_passed

		os.chdir(self.srcdir)

		retval = subprocess.call([os.environ.get('EDITOR') or 'nano', 'PKGBUILD'])
		if 'y' != input('Did PKGBUILD for {} pass review? [y/n] '.format(self.name)).lower():
			return self.set_review_state(False)

		if os.path.exists('{}.install'.format(self.name)):
			retval = subprocess.call([os.environ.get('EDITOR') or 'nano', '{}.install'.format(self.name)])
			if 'y' != input('Did {}.install pass review? [y/n] '.format(self.name)).lower():
				return self.set_review_state(False)

		return self.set_review_state(True)

	def cleanup(self):
		def errhandler(func, path, excinfo):
			os.chmod(path, stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR)
			if os.path.isdir(path):
				os.rmdir(path)
			else:
				os.remove(path)

		if self.srcdir:
			try:
				shutil.rmtree(self.srcdir, onerror=errhandler)
			except PermissionError:
				utils.logerr(None, "Cannot remove {}: Permission denied".format(self.srcdir))

			self.srcdir = None  # if we couldn't remove it, we can't next time, so we ignore the exception and continue


class Package:

	def __init__(self, name, ctx=None, firstparent=None, debug=False):
		self.ctx               = ctx
		self.name              = name
		self.installed         = pacman.is_installed(name)
		self.deps              = []
		self.makedeps          = []
		self.optdeps           = []
		self.parents           = [firstparent] if firstparent else []
		self.built_pkgs        = []
		self.version_installed = pacman.installed_version(name) if self.installed else None
		self.in_repos          = pacman.in_repos(name)
		self.srcpkg            = None
		utils.logmsg(self.ctx.v, 3, "Instantiating package {}".format(self.name))

		self.pkgdata = utils.query_aur("info", self.name, single=True)
		self.in_aur = not self.in_repos and self.pkgdata

		utils.logmsg(self.ctx.v, 4, 'Package details: {}; {}; {}'.format(name, "installed" if self.installed else "not installed", "in repos" if self.in_repos else "not in repos"))

		if self.in_aur:
			self.version_latest    = self.pkgdata['Version']

			if "Depends" in self.pkgdata:
				for pkg in self.pkgdata["Depends"]:
					self.deps.append(parse_dep_pkg(pkg, self.ctx))

			if "MakeDepends" in self.pkgdata:
				for pkg in self.pkgdata["MakeDepends"]:
					self.makedeps.append(parse_dep_pkg(pkg, ctx))

			if "OptDepends" in self.pkgdata:
				for pkg in self.pkgdata["OptDepends"]:
					self.optdeps.append(pkg)

			self.srcpkg = parse_src_pkg(self.pkgdata["PackageBase"], self.pkgdata["Version"], self.pkgdata["URLPath"], ctx=ctx)

			self.srcpkg.download()
			self.srcpkg.extract()

	def review(self):
		utils.logmsg(self.ctx.v, 3, "reviewing {}".format(self.name))
		for dep in self.deps + self.makedeps:
			if not dep.review():
				return False  # already one dep not passing review is killer, no need to process further

		if self.in_repos:
			utils.logmsg(self.ctx.v, 3, "{} passed review: in_repos".format(self.name))
			return True

		if self.installed:
			if not self.in_aur:
				utils.logmsg(self.ctx.v, 3, "{} passed review: installed and not in aur".format(self.name))
				return True
			elif self.version_installed == self.version_latest:
				utils.logmsg(self.ctx.v, 3, "{} passed review: installed in latest version".format(self.name))
				return True

		if self.srcpkg.reviewed:
			utils.logmsg(self.ctx.v, 3, "{} passed review due to positive pre-review".format(self.name))
			return self.srcpkg.review_passed

		if self.in_aur and len(pkg_in_cache(self)) > 0:
			utils.logmsg(self.ctx.v, 3, "{} passed review: in cache".format(self.name))
			return True

		return self.srcpkg.review()

	def build(self, buildflags=['-Cdf'], recursive=False):
		if recursive:
			for d in self.deps:
				succeeded = d.build(buildflags=buildflags, recursive=True)
				if not succeeded:
					return False  # one dep fails, the entire branch fails immediately, software will not be runnable

		if self.in_repos or (self.installed and not self.in_aur):
			return True

		if self.installed and self.in_aur and self.version_installed == self.version_latest:
			return True

		pkgs = pkg_in_cache(self)
		if len(pkgs) > 0:
			self.built_pkgs.append(pkgs[0]) # we only need one of them, not all, if multiple ones with different extensions have been built
			return True

		utils.logmsg(self.ctx.v, 3, "building sources of {}".format(self.name))
		if self.srcpkg.built:
			return self.srcpkg.build_success

		succeeded = self.srcpkg.build(buildflags=buildflags)
		if not succeeded:
			utils.logerr(None, "Building sources of package {} failed, aborting this subtree".format(self.name))
			return False

		pkgext = os.environ.get('PKGEXT') or 'tar.xz'
		fullpkgname_x86_64 = "{}-{}-x86_64.pkg.{}".format(self.name, self.version_latest, pkgext)
		fullpkgname_any = "{}-{}-any.pkg.{}".format(self.name, self.version_latest, pkgext)
		if fullpkgname_x86_64 in os.listdir(self.srcpkg.srcdir):
			self.built_pkgs.append(fullpkgname_x86_64)
			shutil.move(os.path.join(self.srcpkg.srcdir, fullpkgname_x86_64), self.ctx.cachedir)
		elif fullpkgname_any in os.listdir(self.srcpkg.srcdir):
			self.built_pkgs.append(fullpkgname_any)
			shutil.move(os.path.join(self.srcpkg.srcdir, fullpkgname_any), self.ctx.cachedir)
		else:
			utils.logerr(None, "Package file {}-{}-{}.pkg.{} was not found in builddir, aborting this subtree".format(self.name, self.version_latest, "{x86_64,any}", pkgext))
			return False

		return True

	def get_repodeps(self):
		if self.in_repos:
			return set()  # pacman will take care of repodep-tree
		else:
			rdeps = set()
			for d in self.deps:
				if d.in_repos:
					rdeps.add(d)
				else:
					rdeps.union(d.get_repodeps())
			return rdeps

	def get_makedeps(self):
		if self.in_repos:
			return set()
		else:
			makedeps = set(self.makedeps)
			for d in self.deps:
				makedeps.union(d.get_makedeps())
			return makedeps

	def get_built_pkgs(self):
		pkgs = set(self.built_pkgs)
		for d in self.deps:
			pkgs.union(d.get_built_pkgs())
		return pkgs

	def get_optdeps(self):
		optdeps = []
		for d in self.deps:
			od = d.get_optdeps()
			if len(od) > 0:
				optdeps += od

		if len(self.optdeps) > 0:
			optdeps.append((self.name, self.optdeps))

		return optdeps

	def remove_sources(self, recursive=True):
		if self.srcpkg:
			self.srcpkg.cleanup()

		if recursive:
			for d in self.deps + self.makedeps:
				d.remove_sources()



	def __str__(self):
		return self.name

	def __repr__(self):
		return str(self)

