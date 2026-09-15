#pragma once

#include <Epub/Page.h>
#include <I18n.h>

#include <memory>
#include <vector>

#include "activities/Activity.h"
#include "util/Dictionary.h"

class DictionaryWordSelectActivity final : public Activity {
 public:
  explicit DictionaryWordSelectActivity(GfxRenderer& renderer, MappedInputManager& mappedInput,
                                        std::unique_ptr<Page> page, int marginLeft, int marginTop)
      : Activity("DictionaryWordSelect", renderer, mappedInput),
        page(std::move(page)),
        marginLeft(marginLeft),
        marginTop(marginTop) {}

  void onEnter() override;
  void loop() override;
  void render(RenderLock&&) override;

 private:
  struct WordBox {
    int16_t x;
    int16_t y;
    int16_t width;
    uint16_t row;
    const char* text;
    EpdFontFamily::Style style;
  };

  enum class Popup : uint8_t { None, Busy, NotFound, Error };

  void extractWords();
  int closestInRow(uint16_t row, int centerX) const;
  int wordAt(int x, int y) const;
  void moveVertical(int direction);
  void performLookup();
  void drawHighlightWithSnapshot();
  void drawHints() const;

  std::unique_ptr<Page> page;
  const int marginLeft;
  const int marginTop;
  int fontId = 0;
  int lineHeight = 0;

  std::vector<WordBox> words;
  int selected = 0;
  uint16_t rowCount = 0;

  Dictionary dict;
  bool dictOpenAttempted = false;
  bool dictOpenOk = false;

  Popup popup = Popup::None;
  StrId popupMsg = StrId::STR_DICT_NOT_FOUND;
  unsigned long popupTime = 0;

  static constexpr size_t SNAPSHOT_PLANE_CAPACITY = 4096;
  std::unique_ptr<uint8_t[]> snapshot;
  size_t snapshotCapacity = 0;
  GfxRenderer::FrameSnapshot highlightSnapshot;
  int snapshotIdx = -1;

  bool confirmPressSeen = false;
};
