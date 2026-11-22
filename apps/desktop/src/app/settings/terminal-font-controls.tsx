import { SETTING_IDS, settingElementId } from './settings-manifest'
import { TerminalFontSetting } from './terminal-font-setting'
import { TerminalFontSizeSetting } from './terminal-font-size-setting'

const ids = SETTING_IDS.appearance

/** Profile-backed browser terminal controls grouped under Appearance. */
export function TerminalFontControls() {
  return (
    <>
      <div id={settingElementId(ids.terminalFont)}>
        <TerminalFontSetting />
      </div>
      <div id={settingElementId(ids.terminalFontSize)}>
        <TerminalFontSizeSetting />
      </div>
    </>
  )
}
