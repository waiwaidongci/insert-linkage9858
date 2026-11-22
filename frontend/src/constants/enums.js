export const ROLE = {admin:'复核主管', analyst:'分析员', viewer:'只读查看'};
export const STATUS = {queued:'排队中', running:'正在计算', complete:'计算完成', failed:'运行失败'};
export const REVIEW = {pending:'待复核', accepted:'已通过', returned:'已退回'};
export const VERDICT = {
  always:'无论怎么补都达标',
  partial:'只有部分补法达标',
  over:'怎么补都超线',
  blocked:'未测项过多，停止穷举',
};
