# Security policy

Anima is pre-alpha and does not yet have supported release branches. Report suspected
vulnerabilities privately to the repository owner instead of opening a public issue.

Capability Plugins are trusted in-process code, not sandboxes. `PluginLoader`
automatically imports every in-tree Plugin entry point, so review every Plugin directory
before deployment. Hosts must keep secrets outside declarative plugin configuration and
expose credentials only through narrowly scoped service implementations.
