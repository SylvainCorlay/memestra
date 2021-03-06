import beniget
import gast as ast
import os
import sys
import warnings

from collections import defaultdict
from memestra.caching import Cache, CacheKeyFactory, RecursiveCacheKeyFactory
from memestra.caching import Format
from memestra.utils import resolve_module

_defs = ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef


class SilentDefUseChains(beniget.DefUseChains):

    def unbound_identifier(self, name, node):
        pass


class ImportResolver(ast.NodeVisitor):

    def __init__(self, decorator, file_path=None, recursive=False, parent=None):
        '''
        Create an ImportResolver that finds deprecated identifiers.

        A deprecated identifier is an identifier which is decorated
        by `decorator', or which uses a deprecated identifier.

        if `recursive' is greater than 0, it considers identifiers
        from imported module, with that depth in the import tree.

        `parent' is used internally to handle imports.
        '''
        self.deprecated = None
        self.decorator = tuple(decorator)
        self.file_path = file_path
        self.recursive = recursive
        if parent:
            self.cache = parent.cache
            self.visited = parent.visited
            self.key_factory = parent.key_factory
        else:
            self.cache = Cache()
            self.visited = set()
            if recursive:
                self.key_factory = RecursiveCacheKeyFactory()
            else:
                self.key_factory = CacheKeyFactory()

    def load_deprecated_from_module(self, module_name):
        module_path = resolve_module(module_name, self.file_path)

        if module_path is None:
            return None

        module_key = self.key_factory(module_path)

        if module_key in self.cache:
            data = self.cache[module_key]
            if data['version'] == Format.version:
                return set(data['deprecated'])
            elif data['generator'] == 'manual':
                warnings.warn(
                    ("skipping module {} because it has an obsolete, "
                     "manually generated, cache file: {}")
                    .format(module_name,
                            module_key.module_hash))
                return []

        with open(module_path) as fd:
            try:
                module = ast.parse(fd.read())
            except UnicodeDecodeError:
                return []
            duc = SilentDefUseChains()
            duc.visit(module)
            anc = beniget.Ancestors()
            anc.visit(module)

            # Collect deprecated functions
            if self.recursive and module_path not in self.visited:
                self.visited.add(module_path)
                resolver = ImportResolver(self.decorator,
                                          self.file_path,
                                          self.recursive,
                                          parent=self)
                resolver.visit(module)
                deprecated_imports = [d for _, _, d in
                                      resolver.get_deprecated_users(duc, anc)]
            else:
                deprecated_imports = []
            deprecated = self.collect_deprecated(module, duc, anc)
            deprecated.update(deprecated_imports)
            dl = {d.name for d in deprecated}
            dl = {d.name for d in deprecated}
            data = {'generator': 'memestra',
                    'deprecated': sorted(dl)}
            self.cache[module_key] = data
            return dl

    def get_deprecated_users(self, defuse, ancestors):
        deprecated_uses = []
        for deprecated_node in self.deprecated:
            for user in defuse.chains[deprecated_node].users():
                user_ancestors = [n
                                  for n in ancestors.parents(user.node)
                                  if isinstance(n, _defs)]
                if any(f in self.deprecated for f in user_ancestors):
                    continue
                deprecated_uses.append((deprecated_node, user,
                                        user_ancestors[-1] if user_ancestors
                                        else user.node))
        return deprecated_uses

    def visit_Import(self, node):
        for alias in node.names:
            deprecated = self.load_deprecated_from_module(alias.name)
            if deprecated is None:
                continue

            for user in self.def_use_chains.chains[alias].users():
                parent = self.ancestors.parents(user.node)[-1]
                if isinstance(parent, ast.Attribute):
                    if parent.attr in deprecated:
                        self.deprecated.add(parent)

    # FIXME: handle relative imports
    def visit_ImportFrom(self, node):
        deprecated = self.load_deprecated_from_module(node.module)
        if deprecated is None:
            return

        aliases = [alias.name for alias in node.names]

        for deprec in deprecated:
            try:
                index = aliases.index(deprec)
                alias = node.names[index]
                for user in self.def_use_chains.chains[alias].users():
                    self.deprecated.add(user.node)
            except ValueError:
                continue

    def visit_Module(self, node):
        duc = SilentDefUseChains()
        duc.visit(node)
        self.def_use_chains = duc

        ancestors = beniget.Ancestors()
        ancestors.visit(node)
        self.ancestors = ancestors

        self.deprecated = self.collect_deprecated(node, duc, ancestors)
        self.generic_visit(node)

    def collect_deprecated(self, node, duc, ancestors):

        deprecated = set()

        for dlocal in duc.locals[node]:
            dnode = dlocal.node
            if not isinstance(dnode, ast.alias):
                continue

            original_path = tuple(dnode.name.split('.'))
            nbterms = len(original_path)

            if original_path == self.decorator[:nbterms]:
                for user in dlocal.users():
                    parents = list(ancestors.parents(user.node))
                    attrs = list(reversed(self.decorator[nbterms:]))
                    while attrs and parents:
                        attr = attrs[-1]
                        parent = parents.pop()
                        if not isinstance(parent, (ast.Attribute)):
                            break
                        if parent.attr != attr:
                            break
                        attrs.pop()

                    # path parsing fails if some attr left
                    if attrs:
                        continue

                    # Only handle decorators attached to a def
                    if not isinstance(parents[-1], _defs):
                        continue
                    deprecated.add(parents[-1])

            elif original_path == self.decorator[-1:]:
                parent = ancestors.parents(dlocal.node)[-1]
                if not isinstance(parent, ast.ImportFrom):
                    continue
                if parent.module != '.'.join(self.decorator[:-1]):
                    continue
                for user in dlocal.users():
                    parent = ancestors.parents(user.node)[-1]
                    if not isinstance(parent, _defs):
                        continue
                    deprecated.add(parent)

        return deprecated


def prettyname(node):
    if isinstance(node, _defs):
        return node.name
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return prettyname(node.value) + '.' + node.attr
    return repr(node)


def memestra(file_descriptor, decorator, file_path=None, recursive=False):
    '''
    Parse `file_descriptor` and returns a list of
    (function, filename, line, colno) tuples. Each elements
    represents a code location where a deprecated function is used.
    A deprecated function is a function flagged by `decorator`, where
    `decorator` is a tuple representing an import path,
    e.g. (module, attribute)

    If `recursive` is set to `True`, deprecated use are
    checked recursively throughout the *whole* module import tree. Otherwise,
    only one level of import is checked.
    '''

    assert not isinstance(decorator, str) and \
           len(decorator) > 1, "decorator is at least (module, attribute)"

    module = ast.parse(file_descriptor.read())
    # Collect deprecated functions
    resolver = ImportResolver(decorator, file_path, recursive)
    resolver.visit(module)

    ancestors = resolver.ancestors
    duc = resolver.def_use_chains

    # Find their users
    formated_deprecated = []
    for deprecated_node, user, _ in resolver.get_deprecated_users(duc, ancestors):
        formated_deprecated.append((prettyname(deprecated_node),
                               getattr(file_descriptor, 'name', '<>'),
                               user.node.lineno,
                               user.node.col_offset))

    formated_deprecated.sort()
    return formated_deprecated


def run():

    import argparse
    from pkg_resources import iter_entry_points

    parser = argparse.ArgumentParser(description='Check decorator usage.')
    parser.add_argument('--decorator', dest='decorator',
                        default='decorator.deprecated',
                        help='Path to the decorator to check')
    parser.add_argument('--recursive', dest='recursive',
                        action='store_true',
                        help='Traverse the whole module hierarchy')
    parser.add_argument('input', type=argparse.FileType('r'),
                        help='file to scan')

    dispatcher = defaultdict(lambda: memestra)
    for entry_point in iter_entry_points(group='memestra.plugins', name=None):
        entry_point.load()(dispatcher)

    args = parser.parse_args()

    _, extension = os.path.splitext(args.input.name)

    deprecate_uses = dispatcher[extension](args.input,
                                           args.decorator.split('.'),
                                           args.input.name,
                                           args.recursive)

    for fname, fd, lineno, colno in deprecate_uses:
        print("{} used at {}:{}:{}".format(fname, fd, lineno, colno + 1))


if __name__ == '__main__':
    run()
