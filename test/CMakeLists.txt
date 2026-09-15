cmake_minimum_required(VERSION 3.16)
project(crosspoint_reader_tests C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

include(FetchContent)

FetchContent_Declare(
  googletest
  GIT_REPOSITORY https://github.com/google/googletest.git
  GIT_TAG v1.17.0
)

set(gtest_force_shared_crt ON CACHE BOOL "" FORCE)
set(INSTALL_GTEST OFF CACHE BOOL "" FORCE)
set(BUILD_GMOCK OFF CACHE BOOL "" FORCE)
FetchContent_MakeAvailable(googletest)

set(REPO_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/..")

add_library(crosspoint_test_common INTERFACE)
target_include_directories(crosspoint_test_common INTERFACE
  ${REPO_ROOT}
  ${REPO_ROOT}/lib
)
target_compile_options(crosspoint_test_common INTERFACE
  -Wall
  -Wextra
  -pedantic
)

enable_testing()
include(GoogleTest)
find_package(Python3 REQUIRED COMPONENTS Interpreter)

add_test(NAME mapped_input
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/mapped_input_test.py
)
add_test(NAME gfx_gray
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/gfx_gray_test.py
)
add_test(NAME ui_font_assets
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/ui_font_assets_test.py
)
add_test(NAME panel_diagnostic
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/panel_diagnostic_test.py
)
add_test(NAME panel_transition
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/panel_transition_test.py
)
add_test(NAME panel_cli_cleanup
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/panel_cli_cleanup_test.py
)
add_test(NAME sdk_patch_hook
  COMMAND ${Python3_EXECUTABLE} -m unittest discover
    -s ${CMAKE_CURRENT_SOURCE_DIR}/sdk_patch_hook -p "test_*.py"
)
add_test(NAME usb_file_transfer
  COMMAND bash ${CMAKE_CURRENT_SOURCE_DIR}/usb_file_transfer/run.sh
)
add_test(NAME usb_serial_transport
  COMMAND bash ${CMAKE_CURRENT_SOURCE_DIR}/usb_serial_transport/run.sh
)
set_tests_properties(mapped_input gfx_gray panel_diagnostic panel_transition usb_file_transfer usb_serial_transport PROPERTIES
  ENVIRONMENT "CXX=${CMAKE_CXX_COMPILER}"
)

add_subdirectory(streaming_json_parser)
add_subdirectory(release_json_parser)
add_subdirectory(differential_rounding)
add_subdirectory(hyphenation_eval)
add_subdirectory(utf8_compose)
add_subdirectory(opds_filename)
add_subdirectory(minibidi_arabic)
add_subdirectory(combining_marks)
add_subdirectory(usb_power_guard)
