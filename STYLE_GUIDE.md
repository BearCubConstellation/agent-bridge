# Agent Bridge 品牌色系

品牌意象：仙鹿 / 雾林清晨 / 剧场递纸条

## 主色系：夜林

| Token        | 亮色值                      | 暗色值                        | 用途           |
|-------------|----------------------------|------------------------------|---------------|
| bg          | `#F0F4F2` 薄雾              | `#1C1F23` 暖炭               | 页面背景       |
| surface     | `#E2E8E5` 苔面              | `#25292E` 炭面               | 卡片/代码块底  |
| paper       | `#1A2722` 深林              | `#E4DFD4` 晨雾               | 主文字/标题    |
| ink         | `#6B7B74` 石苔              | `#7E8388` 灰                 | 次要信息/标签  |

## 副色系：苔绿

| Token        | 亮色值                      | 暗色值                        | 用途           |
|-------------|----------------------------|------------------------------|---------------|
| moss        | `#5B9A4A` 鲜苔              | `#7DAA6E` 苔绿               | 强调/品牌色    |
| moss-hi     | `#4E8A3F` 深苔              | `#A0C98F` 新芽               | hover/交互     |
| moss-lo     | `#A8C89E` 浅苔              | `#3D5A3A` 深林               | 装饰/fill     |
| border      | `rgba(61,90,58,0.12)`      | `rgba(255,255,255,0.08)`     | 网格/边框      |
| glow        | `rgba(91,154,74,0.08)`     | `rgba(125,170,110,0.06)`     | 顶部渐变光晕   |

## 字族

- **标题**: DM Serif Display（衬线）
- **代码/标签**: Courier Prime（等宽）
- **正文**: 系统默认（-apple-system / PingFang SC / Noto Sans SC）

## 设计原则

- 亮色为默认，暗色为可选。两种模式独立色系，不做简单亮度翻转
- 苔绿只用于强调（标题、高亮、交互态），不染底色
- 暗色底是中性暖炭 `#1C1F23`，不是深绿墨
- 全大写宽字距标签 + 衬线标题 + 等宽代码 = 编辑/剧场排版感

## CSS 变量参考

```css
/* 亮色（默认） */
:root {
  --bg: #F0F4F2;
  --surface: #E2E8E5;
  --paper: #1A2722;
  --ink: #6B7B74;
  --moss: #5B9A4A;
  --moss-hi: #4E8A3F;
  --moss-lo: #A8C89E;
  --border: rgba(61, 90, 58, 0.12);
  --border-strong: rgba(61, 90, 58, 0.2);
  --paper-dim: rgba(26, 39, 34, 0.6);
  --paper-faint: rgba(26, 39, 34, 0.2);
  --glow: linear-gradient(180deg, rgba(91,154,74,0.08) 0%, transparent 100%);
  --serif: 'DM Serif Display', Georgia, serif;
  --mono: 'Courier Prime', 'Courier New', Courier, monospace;
  --sans: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Noto Sans SC', sans-serif;
}

/* 暗色 */
:root.light {
  --bg: #1C1F23;
  --surface: #25292E;
  --paper: #E4DFD4;
  --ink: #7E8388;
  --moss: #7DAA6E;
  --moss-hi: #A0C98F;
  --moss-lo: #3D5A3A;
  --border: rgba(255, 255, 255, 0.08);
  --border-strong: rgba(255, 255, 255, 0.14);
  --paper-dim: rgba(228, 223, 212, 0.55);
  --paper-faint: rgba(228, 223, 212, 0.2);
  --glow: linear-gradient(180deg, rgba(125,170,110,0.06) 0%, transparent 100%);
}
```
