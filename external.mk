# AgentOS — overrides para el árbol BR2_EXTERNAL
#
# jpeg-turbo 2.1.5 usa cmake_minimum_required < 3.5, que CMake 3.28+
# (Ubuntu 24.04) ya no soporta. Este override añade la flag necesaria.
JPEG_TURBO_CONF_OPTS += -DCMAKE_POLICY_VERSION_MINIMUM=3.5
