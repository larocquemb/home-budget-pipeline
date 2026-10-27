/* Give the LaunchAgent a distinct signed executable while using the venv's Python. */
#include <Python.h>
int main(int argc, char **argv) { return Py_BytesMain(argc, argv); }
