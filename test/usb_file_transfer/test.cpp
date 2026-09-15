#include <algorithm>
#include <cstdlib>
#include <iostream>
#include <map>
#include <string>
#include <vector>

#include "UsbFileTransfer.h"

using usb_transfer::StorageBackend;
using usb_transfer::Tx;
using usb_transfer::UsbFileTransfer;

static void check(bool condition, const char* message) {
  if (!condition) {
    std::cerr << "FAIL: " << message << '\n';
    std::exit(1);
  }
}

struct MemoryStorage : StorageBackend {
  std::map<std::string, std::vector<uint8_t>> files;
  std::string opened;
  std::string fail;
  size_t position = 0;
  size_t writes = 0;
  bool corrupt = false;

  bool exists(const std::string& path, bool& result) override {
    result = files.count(path) != 0;
    return fail != "exists";
  }
  bool makeDirectory(const std::string&) override { return fail != "mkdir"; }
  bool createExclusive(const std::string& path) override {
    if (fail == "create" || files.count(path)) return false;
    files[path] = {};
    opened = path;
    position = 0;
    return true;
  }
  bool openRead(const std::string& path) override {
    if (fail == "open" || !files.count(path)) return false;
    opened = path;
    position = 0;
    return true;
  }
  bool size(uint32_t& result) override {
    result = static_cast<uint32_t>(files[opened].size());
    return fail != "size";
  }
  bool write(const uint8_t* data, size_t length, size_t& written) override {
    ++writes;
    written = fail == "short-write" ? length - 1 : length;
    if (fail == "write") return false;
    files[opened].insert(files[opened].end(), data, data + written);
    return true;
  }
  bool read(uint8_t* data, size_t length, size_t& received) override {
    if (fail == "read") return false;
    const auto& bytes = files[opened];
    received = std::min(length, bytes.size() - position);
    std::copy_n(bytes.data() + position, received, data);
    position += received;
    if (corrupt && received) data[0] ^= 1;
    return true;
  }
  bool flush() override { return fail != "flush"; }
  bool close() override {
    opened.clear();
    return fail != "close";
  }
  bool remove(const std::string& path) override {
    if (fail == "remove") return false;
    files.erase(path);
    return true;
  }
  bool renameNoReplace(const std::string& from, const std::string& to) override {
    if (fail == "rename" || files.count(to)) return false;
    files[to] = files.at(from);
    files.erase(from);
    return true;
  }
};

struct CaptureTx : Tx {
  std::vector<std::string> lines;
  bool broken = false;
  bool stream = false;
  bool sendLine(const std::string& line) override {
    lines.push_back(line);
    if (stream) std::cout << line << std::endl;
    return !broken;
  }
};

static std::string hex(const std::string& bytes) {
  const char* digits = "0123456789abcdef";
  std::string output;
  for (unsigned char byte : bytes) {
    output += digits[byte >> 4];
    output += digits[byte & 15];
  }
  return output;
}

struct Fixture {
  MemoryStorage storage;
  CaptureTx tx;
  UsbFileTransfer transfer{storage, tx};
  std::string command(const std::string& line) {
    check(transfer.pollLine(line), "protocol line must be consumed");
    return tx.lines.back();
  }
  std::string put(const std::string& root = "/Classics", const std::string& name = "book.epub",
                  const std::string& size = "9", const std::string& crc = "cbf43926") {
    return command("CP1 PUT " + hex(root) + " " + hex(name) + " " + size + " " + crc);
  }
  void upload() {
    check(put() == "CP1 READY 512", "upload begins with bounded chunks");
    check(command("CP1 DATA 0 313233343536373839") == "CP1 ACK 0", "known payload acknowledged");
  }
  void commit() {
    check(command("CP1 COMMIT") == "CP1 VERIFY 0 9", "commit starts asynchronous verification");
    transfer.tick(1);
    check(command("CP1 STATUS") == "CP1 COMPLETE 9 cbf43926", "stored CRC and size match published file");
  }
};

static void successAndRetry() {
  Fixture f;
  check(!f.transfer.pollLine("log output"), "idle dispatcher passes non-protocol lines");
  f.upload();
  check(f.command("CP1 DATA 0 313233343536373839") == "CP1 ACK 0", "identical retry is acknowledged");
  check(f.storage.writes == 1, "retry does not append again");
  check(f.command("CP1 DATA 0 303233343536373839") == "CP1 ERROR SEQUENCE", "conflicting retry rejected");
  check(f.command("CP1 DATA 2 31") == "CP1 ERROR SEQUENCE", "out of order rejected");
  check(f.command("screenshot") == "CP1 ERROR BUSY", "active transfer excludes unrelated commands");
  f.commit();
  check(!f.transfer.active(), "completion releases active state");
  check(f.command("CP1 COMMIT") == "CP1 COMPLETE 9 cbf43926", "commit acknowledgment can be retried");
  check(f.storage.files.size() == 1, "only published destination remains");
  check(
      f.storage.files.at("/Classics/book.epub") == std::vector<uint8_t>({'1', '2', '3', '4', '5', '6', '7', '8', '9'}),
      "published bytes equal independently specified payload");
  check(f.put() == "CP1 ERROR EXISTS", "existing book is never overwritten");
  check(f.command("CP1 GET " + hex("/Classics") + " " + hex("book.epub")) == "CP1 READABLE 9", "readback size");
  check(f.command("CP1 READ 0") == "CP1 CHUNK 0 313233343536373839", "readback contains stored bytes");
  check(f.command("CP1 READ 0") == "CP1 CHUNK 0 313233343536373839", "readback retry returns same bytes");
  check(f.command("CP1 READ 1") == "CP1 EOF 1", "readback length is exact");
  check(f.command("CP1 DONE") == "CP1 CLOSED", "readback closes explicitly");
  check(f.command("CP1 DONE") == "CP1 CLOSED", "lost close acknowledgment is retryable");
}

static void rejectPathsAndControls() {
  const std::vector<std::pair<std::string, std::string>> bad = {{"/other", "book.epub"},
                                                                {"/Classics/child", "book.epub"},
                                                                {"/fonts/a/b", "font.cpfont"},
                                                                {"/fonts/..", "font.cpfont"},
                                                                {"/Classics", "../book.epub"},
                                                                {"/Classics", "/book.epub"},
                                                                {"/Classics", "a\\b.epub"},
                                                                {"/Classics", "a..epub"},
                                                                {"/Classics", "bad?.epub"},
                                                                {"/Classics", "font.cpfont"},
                                                                {"/fonts", "book.epub"},
                                                                {"/fonts", "notes.txt"},
                                                                {"/Classics", "bad\n.epub"},
                                                                {"/Classics", std::string(129, 'a') + ".epub"},
                                                                {"/Classics", std::string("\xc3\xa9.epub")}};
  for (const auto& path : bad) {
    Fixture f;
    check(f.put(path.first, path.second) == "CP1 ERROR PATH", "unsafe path rejected");
    check(f.storage.files.empty(), "unsafe path creates no files");
  }
  for (const auto& name : {"OFL.txt", "LICENSE.txt", "LICENSE.md", "reader.cpfont"}) {
    Fixture f;
    check(f.put("/fonts/Reading", name) == "CP1 READY 512", "font and validated license path accepted");
    check(f.command("CP1 ABORT") == "CP1 ABORTED", "accepted upload can abort");
  }
  for (const auto& line : {"CP1", "CP1 PUT", "CP1 STATUS extra", "CP1 DATA 0 gg", "CP1 WHAT"}) {
    Fixture f;
    check(f.command(line).find("CP1 ERROR ") == 0, "malformed control rejected");
  }
  for (const auto& size : {"0", "33554433", "4294967296", "-1", "9x"}) {
    Fixture f;
    check(f.put("/Classics", "book.epub", size) == "CP1 ERROR SIZE", "invalid size rejected");
  }
  Fixture f;
  check(f.put("/Classics", "book.epub", "9", "xyz") == "CP1 ERROR CRC", "malformed CRC rejected");
}

static void failureCleanup() {
  {
    Fixture f;
    f.put();
    check(f.command("CP1 COMMIT") == "CP1 ERROR LENGTH", "truncated upload cannot publish");
    check(f.storage.files.empty(), "truncated upload cleaned up");
  }
  {
    Fixture f;
    f.upload();
    f.storage.corrupt = true;
    f.command("CP1 COMMIT");
    f.transfer.tick(1);
    check(f.command("CP1 STATUS") == "CP1 ERROR CHECKSUM", "stored corruption fails independent CRC");
    check(f.storage.files.empty(), "corrupt upload cleaned up");
  }
  for (const auto& line : {std::string("CP1 DATA 0 0"), std::string("CP1 DATA 0 gg"),
                           "CP1 DATA 0 " + std::string(1026, 'a'), "CP1 DATA 0 " + std::string(20, 'a')}) {
    Fixture f;
    f.put();
    check(f.command(line).find("CP1 ERROR ") == 0, "invalid data rejected");
    check(!f.transfer.active() && f.storage.files.empty(), "invalid data aborts exclusive temporary");
  }
  for (const auto& operation : {"write", "short-write", "flush", "close", "open", "size", "read", "rename"}) {
    Fixture f;
    f.put();
    f.storage.fail = operation;
    f.command("CP1 DATA 0 313233343536373839");
    if (f.transfer.active()) f.command("CP1 COMMIT");
    f.transfer.tick(1);
    check(!f.storage.files.count("/Classics/book.epub"), "storage failure cannot publish destination");
    check(f.storage.files.empty(), "storage failure removes owned temporary");
  }
  {
    Fixture f;
    f.storage.files["/Classics/book.epub.cp1.part"] = {'x'};
    check(f.put() == "CP1 ERROR IO", "exclusive temporary collision rejected");
    f.transfer.abort();
    check(f.storage.files.at("/Classics/book.epub.cp1.part") == std::vector<uint8_t>{'x'},
          "unowned temporary is never removed");
  }
  {
    Fixture f;
    f.upload();
    f.storage.fail = "remove";
    check(!f.transfer.abort(), "cleanup error is reported");
    check(f.transfer.active(), "failed cleanup retains transfer ownership");
    f.storage.fail.clear();
    check(f.transfer.abort() && f.storage.files.empty(), "cleanup can be retried");
  }
  {
    Fixture f;
    f.upload();
    f.transfer.tick(29999);
    check(f.transfer.active(), "idle timeout has not elapsed");
    f.transfer.tick(30000);
    check(!f.transfer.active() && f.storage.files.empty(), "idle timeout aborts and cleans");
    check(f.command("CP1 STATUS") == "CP1 ERROR TIMEOUT", "timeout visible to host");
  }
  {
    Fixture f;
    f.put();
    f.tx.broken = true;
    f.command("CP1 DATA 0 313233343536373839");
    check(!f.transfer.active() && f.storage.files.empty(), "failed TX aborts upload");
  }
}

static void boundariesAndReadbackErrors() {
  for (const auto& operation : {"exists", "mkdir", "create"}) {
    Fixture f;
    f.storage.fail = operation;
    check(f.put() == "CP1 ERROR IO", "begin IO failure reported");
    check(!f.transfer.active() && f.storage.files.empty(), "begin failure owns no file");
  }
  {
    Fixture f;
    check(f.put("/Classics", "book.epub", "33554432") == "CP1 READY 512", "maximum file size accepted");
    f.transfer.abort();
    f.transfer.tick(0xfffffff0U);
    f.upload();
    f.transfer.tick(29983);
    check(f.transfer.active(), "timeout subtraction handles timestamp rollover");
    f.transfer.tick(29984);
    check(!f.transfer.active(), "timeout fires across timestamp rollover");
  }
  {
    Fixture f;
    f.upload();
    f.storage.files.begin()->second.pop_back();
    check(f.command("CP1 COMMIT") == "CP1 ERROR LENGTH", "stored truncation rejected before CRC scan");
    check(f.storage.files.empty(), "stored truncation cleaned up");
  }
  {
    Fixture f;
    f.upload();
    f.command("CP1 COMMIT");
    f.storage.files["/Classics/book.epub"] = {'x'};
    f.transfer.tick(1);
    check(f.command("CP1 STATUS") == "CP1 ERROR IO", "destination race rejects publication");
    check(f.storage.files.size() == 1 && f.storage.files.at("/Classics/book.epub") == std::vector<uint8_t>{'x'},
          "destination race preserves existing file and removes temporary");
  }
  for (const auto& operation : {"open", "size", "read", "close"}) {
    Fixture f;
    f.upload();
    f.commit();
    if (std::string(operation) == "open" || std::string(operation) == "size") f.storage.fail = operation;
    f.command("CP1 GET " + hex("/Classics") + " " + hex("book.epub"));
    if (f.transfer.active()) {
      if (std::string(operation) == "read") f.storage.fail = operation;
      f.command("CP1 READ 0");
      if (f.transfer.active()) {
        f.command("CP1 READ 1");
        f.storage.fail = operation;
        check(f.command("CP1 DONE") == "CP1 ERROR IO", "download close failure reported");
      }
    }
    check(!f.transfer.active(), "download IO failure releases active handle");
    check(f.storage.files.at("/Classics/book.epub").size() == 9, "download failure preserves source");
  }
  {
    Fixture f;
    f.upload();
    f.commit();
    f.command("CP1 GET " + hex("/Classics") + " " + hex("book.epub"));
    f.storage.files.at("/Classics/book.epub").pop_back();
    check(f.command("CP1 READ 0") == "CP1 ERROR IO", "readback short read is an error");
    check(f.storage.files.size() == 1, "short read does not remove source");
  }
}

int main(int argc, char** argv) {
  if (argc == 2 && std::string(argv[1]) == "--serve") {
    Fixture f;
    f.tx.stream = true;
    std::string line;
    uint32_t now = 0;
    while (std::getline(std::cin, line)) {
      f.transfer.pollLine(line);
      f.transfer.tick(++now);
    }
    return 0;
  }
  successAndRetry();
  rejectPathsAndControls();
  failureCleanup();
  boundariesAndReadbackErrors();
  std::cout << "PASS: USB transfer success, retries, path validation, corruption, IO and cleanup\n";
}
