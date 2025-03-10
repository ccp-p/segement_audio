import os
import tempfile
import shutil
import time
import concurrent.futures
import logging
import requests
import signal
from typing import Dict, List, Set, Tuple, Optional, Any, Callable
from pathlib import Path
from pydub import AudioSegment
import subprocess
from tqdm import tqdm

# 导入工具函数
from utils import format_time_duration, load_json_file, save_json_file, ProgressBar, LogConfig

# 导入ASR模块和ASR管理器
from asr import ASRDataSeg
from asr_manager import ASRManager
from text_formatter import TextFormatter

class AudioProcessor:
    """音频处理类，负责音频分割、转写和文本整合"""
    
    def __init__(self, **kwargs):
        """
        音频处理器初始化
        
        Args:
            **kwargs: 配置参数
        """
        # 从kwargs获取参数，若不存在则使用默认值
        self.media_folder = kwargs.get('media_folder', './media')
        self.output_folder = kwargs.get('output_folder', './output')
        self.max_retries = kwargs.get('max_retries', 3)
        self.max_workers = kwargs.get('max_workers', 4)
        self.use_jianying_first = kwargs.get('use_jianying_first', True)
        self.use_kuaishou = kwargs.get('use_kuaishou', True)
        self.use_bcut = kwargs.get('use_bcut', True)
        self.format_text = kwargs.get('format_text', True)
        self.include_timestamps = kwargs.get('include_timestamps', True)
        self.show_progress = kwargs.get('show_progress', True)
        self.process_video = kwargs.get('process_video', True)
        self.video_extensions = kwargs.get('video_extensions', ['.mp4', '.mov', '.avi'])
        self.extract_audio_only = kwargs.get('extract_audio_only', False)
        
        # 创建输出目录
        os.makedirs(self.output_folder, exist_ok=True)
        
        # 记录文件路径
        self.processed_record_file = os.path.join(self.output_folder, "processed_audio_files.json")
        self.processed_files = load_json_file(self.processed_record_file)
        
        # 初始化中断信号处理
        self.interrupt_received = False
        self.original_sigint_handler = signal.getsignal(signal.SIGINT)
        
        # 临时目录
        self.temp_dir = tempfile.mkdtemp()
        self.temp_segments_dir = os.path.join(self.temp_dir, "segments")
        os.makedirs(self.temp_segments_dir, exist_ok=True)
        
        # 初始化ASR服务管理器
        self.asr_manager = ASRManager(
            use_jianying_first=self.use_jianying_first,
            use_kuaishou=self.use_kuaishou,
            use_bcut=self.use_bcut
        )
        
        # 进度条相关
        self.progress_bars: Dict[str, ProgressBar] = {}
    
    # 新增: 通用进度条管理方法
    def create_progress_bar(self, name: str, total: int, prefix: str, suffix: str = "") -> Optional[ProgressBar]:
        """
        创建并存储一个进度条
        
        Args:
            name: 进度条名称，用于后续引用
            total: 总步数
            prefix: 进度条前缀
            suffix: 进度条后缀
            
        Returns:
            创建的进度条，如果show_progress为False则返回None
        """
        if not self.show_progress:
            return None
            
        progress_bar = ProgressBar(total=total, prefix=prefix, suffix=suffix)
        self.progress_bars[name] = progress_bar
        return progress_bar
    
    def update_progress(self, name: str, current: Optional[int] = None, suffix: Optional[str] = None) -> None:
        """
        更新指定进度条
        
        Args:
            name: 进度条名称
            current: 当前进度
            suffix: 新的后缀文本
        """
        if not self.show_progress or name not in self.progress_bars:
            return
            
        self.progress_bars[name].update(current, suffix)
    
    def finish_progress(self, name: str, suffix: Optional[str] = None) -> None:
        """
        完成指定进度条
        
        Args:
            name: 进度条名称
            suffix: 完成时的后缀文本
        """
        if not self.show_progress or name not in self.progress_bars:
            return
            
        self.progress_bars[name].finish(suffix)
        del self.progress_bars[name]
    
    # 新增: 安全执行函数的包装器
    def safe_execute(self, func: Callable, error_msg: str = "执行出错", progress_name: Optional[str] = None, 
                  error_suffix: Optional[str] = None, *args, **kwargs) -> Any:
        """
        安全执行函数，处理异常并更新进度条
        
        Args:
            func: 要执行的函数
            error_msg: 出错时的日志消息
            progress_name: 相关进度条名称
            error_suffix: 出错时的进度条后缀，默认使用错误消息
            args, kwargs: 传递给func的参数
            
        Returns:
            函数执行结果，出错时返回None
        """
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if error_suffix is None:
                error_suffix = f"失败 - {str(e)}"
                
            logging.error(f"{error_msg}: {str(e)}")
            
            if progress_name and self.show_progress and progress_name in self.progress_bars:
                self.finish_progress(progress_name, error_suffix)
                
            return None
    
    def _save_processed_records(self):
        """保存已处理文件记录"""
        save_json_file(self.processed_record_file, self.processed_files)
    
    def handle_interrupt(self, sig, frame):
        """处理中断信号"""
        logging.warning("\n\n⚠️ 接收到中断信号，正在安全终止程序...\n稍等片刻，正在保存已处理的数据...\n")
        self.interrupt_received = True
        # 不立即退出，允许程序完成当前处理和清理
    
    def split_audio_file(self, input_path: str, segment_length: int = 30) -> List[str]:
        """
        将单个音频文件分割为较小片段
        
        Args:
            input_path: 输入音频文件路径
            segment_length: 每个片段的长度(秒)
            
        Returns:
            分割后的片段文件列表
        """
        filename = os.path.basename(input_path)
        logging.info(f"正在分割 {filename} 为小片段...")
        
        # 创建进度条，但先不更新
        progress_name = f"split_{filename}"
        
        def do_split():
            audio = AudioSegment.from_mp3(input_path)
            
            # 计算总时长（毫秒转秒）
            total_duration = len(audio) // 1000
            logging.info(f"音频总时长: {total_duration}秒")
            
            # 预计片段数
            expected_segments = (total_duration + segment_length - 1) // segment_length
            
            # 创建分割进度条
            self.create_progress_bar(
                progress_name,
                total=expected_segments, 
                prefix=f"分割 {filename}", 
                suffix="准备中"
            )
            
            segment_files = []
            
            # 分割音频
            for i, start in enumerate(range(0, total_duration, segment_length)):
                end = min(start + segment_length, total_duration)
                segment = audio[start*1000:end*1000]
                
                # 导出为WAV格式（兼容语音识别API）
                output_filename = f"{os.path.splitext(filename)[0]}_part{i+1:03d}.wav"
                output_path = os.path.join(self.temp_segments_dir, output_filename)
                
                # 更新进度条
                self.update_progress(
                    progress_name, 
                    i, 
                    f"导出片段 {i+1}/{expected_segments}"
                )
                
                segment.export(
                    output_path,
                    format="wav",
                    parameters=["-ac", "1", "-ar", "16000"]  # 单声道，16kHz采样率
                )
                segment_files.append(output_filename)
                logging.debug(f"  ├─ 分割完成: {output_filename}")
            
            # 完成进度条
            self.finish_progress(progress_name, f"完成 - {len(segment_files)} 个片段")
            
            return segment_files
        
        # 使用安全执行器处理错误
        result = self.safe_execute(
            do_split, 
            error_msg=f"分割音频文件 {filename} 失败",
            progress_name=progress_name
        )
        
        return result or []
    
    def recognize_audio(self, audio_path: str) -> Optional[str]:
        """
        识别单个音频片段
        
        Args:
            audio_path: 音频文件路径
            
        Returns:
            识别结果文本，失败返回None
        """
        # 使用ASR管理器进行识别
        return self.asr_manager.recognize_audio(audio_path)
    
    def process_audio_segments(self, segment_files: List[str]) -> Dict[int, str]:
        """
        使用并行处理识别多个音频片段
        
        Args:
            segment_files: 音频片段文件名列表
            
        Returns:
            识别结果字典，格式为 {片段索引: 识别文本}
        """
        segment_results: Dict[int, str] = {}
        
        logging.info(f"开始多线程识别 {len(segment_files)} 个音频片段...")
        
        # 创建识别进度条
        recognize_progress = None
        if self.show_progress:
            recognize_progress = ProgressBar(
                total=len(segment_files), 
                prefix="识别进度", 
                suffix=f"0/{len(segment_files)} 片段完成"
            )
            
        # 使用线程池并行处理音频片段
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # 创建任务字典，映射片段索引和对应的Future对象
            future_to_segment = {
                executor.submit(self.recognize_audio, os.path.join(self.temp_segments_dir, segment_file)): 
                (i, segment_file)
                for i, segment_file in enumerate(segment_files)
            }
            
            # 收集结果，并添加中断检查
            try:
                completed_count = 0
                for future in concurrent.futures.as_completed(future_to_segment):
                    if self.interrupt_received:
                        logging.warning("检测到中断，正在取消剩余任务...")
                        executor.shutdown(wait=False, cancel_futures=True)
                        break
                        
                    i, segment_file = future_to_segment[future]
                    try:
                        text = future.result(timeout=60)  # 添加超时以避免无限等待
                        completed_count += 1
                        
                        if text:
                            segment_results[i] = text
                            status_text = f"{completed_count}/{len(segment_files)} 片段完成 (成功识别 {len(segment_results)})"
                            logging.debug(f"  ├─ 成功识别: {segment_file}")
                        else:
                            status_text = f"{completed_count}/{len(segment_files)} 片段完成 (失败 {completed_count - len(segment_results)})"
                            logging.warning(f"  ├─ 识别失败: {segment_file}")
                        
                        # 更新进度条
                        if recognize_progress:
                            recognize_progress.update(completed_count, status_text)
                            
                    except concurrent.futures.TimeoutError:
                        completed_count += 1
                        logging.warning(f"  ├─ 识别超时: {segment_file}")
                        if recognize_progress:
                            recognize_progress.update(
                                completed_count, 
                                f"{completed_count}/{len(segment_files)} 片段完成 (超时 {segment_file})"
                            )
                    except Exception as exc:
                        completed_count += 1
                        logging.error(f"  ├─ 识别出错: {segment_file} - {str(exc)}")
                        if recognize_progress:
                            recognize_progress.update(
                                completed_count, 
                                f"{completed_count}/{len(segment_files)} 片段完成 (错误)"
                            )
            except KeyboardInterrupt:
                logging.warning("检测到用户中断，正在取消剩余任务...")
                executor.shutdown(wait=False, cancel_futures=True)
                self.interrupt_received = True
            
        # 完成进度条
        if recognize_progress:
            success_count = len(segment_results)
            fail_count = len(segment_files) - success_count
            recognize_progress.finish(
                f"完成 - {success_count} 成功, {fail_count} 失败" + 
                (" (已中断)" if self.interrupt_received else "")
            )
        
        return segment_results
    
    def retry_failed_segments(self, segment_files: List[str], 
                             segment_results: Dict[int, str]) -> Dict[int, str]:
        """
        重试识别失败的片段
        
        Args:
            segment_files: 所有音频片段文件名列表
            segment_results: 已成功识别的结果
            
        Returns:
            更新后的识别结果字典
        """
        # 如果没有中断并且有失败的片段，则进行重试
        if self.interrupt_received:
            return segment_results
            
        fail_count = len(segment_files) - len(segment_results)
        if fail_count == 0:
            return segment_results
            
        logging.info(f"\n开始重试 {fail_count} 个失败的片段...")
        failed_segments = [(i, segment_files[i]) for i in range(len(segment_files)) 
                         if i not in segment_results]
        
        for retry_round in range(1, self.max_retries + 1):
            if not failed_segments or self.interrupt_received:
                break
                
            logging.info(f"第 {retry_round} 轮重试 ({len(failed_segments)} 个片段):")
            
            # 创建重试进度条
            retry_progress = None
            if self.show_progress:
                retry_progress = ProgressBar(
                    total=len(failed_segments), 
                    prefix=f"重试 #{retry_round}", 
                    suffix=f"0/{len(failed_segments)} 片段完成"
                )
            
            still_failed = []
            success_in_round = 0
            completed_count = 0
            
            # 对失败的片段进行多线程重试
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as retry_executor:
                future_to_failed = {
                    retry_executor.submit(self.recognize_audio, 
                                        os.path.join(self.temp_segments_dir, segment_file)): 
                    (idx, segment_file)
                    for idx, segment_file in failed_segments
                }
                
                try:
                    for future in concurrent.futures.as_completed(future_to_failed):
                        if self.interrupt_received:
                    (idx, segment_file)
                    for idx, segment_file in failed_segments
                }
                
                try:
                    for future in concurrent.futures.as_completed(future_to_failed):
                        if self.interrupt_received:
                            logging.warning("检测到中断，正在取消剩余重试任务...")
                            retry_executor.shutdown(wait=False, cancel_futures=True)
                            break
                            
                        idx, segment_file = future_to_failed[future]
                        completed_count += 1
                        
                        try:
                            text = future.result(timeout=60)
                            if text:
                                segment_results[idx] = text
                                success_in_round += 1
                                logging.debug(f"  ├─ 重试成功: {segment_file}")
                                
                                # 更新进度条
                                if retry_progress:
                                    retry_progress.update(
                                        completed_count, 
                                        f"{completed_count}/{len(failed_segments)} 完成 (成功 {success_in_round})"
                                    )
                            else:
                                still_failed.append((idx, segment_file))
                                logging.warning(f"  ├─ 重试失败: {segment_file}")
                                
                                # 更新进度条
                                if retry_progress:
                                    retry_progress.update(
                                        completed_count, 
                                        f"{completed_count}/{len(failed_segments)} 完成 (失败 {len(still_failed)})"
                                    )
                        except concurrent.futures.TimeoutError:
                            still_failed.append((idx, segment_file))
                            logging.warning(f"  ├─ 重试超时: {segment_file}")
                            
                            # 更新进度条
                            if retry_progress:
                                retry_progress.update(
                                    completed_count, 
                                    f"{completed_count}/{len(failed_segments)} 完成 (超时 {len(still_failed)})"
                                )
                        except Exception as exc:
                            still_failed.append((idx, segment_file))
                            logging.error(f"  ├─ 重试出错: {segment_file} - {str(exc)}")
                            
                            # 更新进度条
                            if retry_progress:
                                retry_progress.update(
                                    completed_count, 
                                    f"{completed_count}/{len(failed_segments)} 完成 (错误 {len(still_failed)})"
                                )
                except KeyboardInterrupt:
                    logging.warning("检测到用户中断，正在取消剩余重试任务...")
                    retry_executor.shutdown(wait=False, cancel_futures=True)
                    self.interrupt_received = True
            
            # 完成进度条
            if retry_progress:
                retry_progress.finish(
                    f"完成 - 成功 {success_in_round}, 仍失败 {len(still_failed)}" +
                    (" (已中断)" if self.interrupt_received else "")
                )
            
            failed_segments = still_failed
            logging.info(f"  └─ 第 {retry_round} 轮重试结果: 成功 {success_in_round}, 仍失败 {len(still_failed)}")
            
            if not still_failed:
                logging.info("  └─ 所有片段都已成功识别，无需继续重试")
                break
        
        return segment_results
    
    def prepare_result_text(self, segment_files: List[str], 
                          segment_results: Dict[int, str]) -> str:
        """
        准备最终的识别结果文本
        
        Args:
            segment_files: 所有音频片段文件名列表
            segment_results: 识别结果字典
            
        Returns:
            合并格式化后的文本
        """
        # 按顺序合并所有文本片段
        all_text = []
        all_timestamps = []
        
        # 显示文本准备进度条
        if self.show_progress:
            text_prep_progress = ProgressBar(
                total=len(segment_files),
                prefix="文本准备",
                suffix="处理中"
            )
        
        for i in range(len(segment_files)):
            if i in segment_results:
                all_text.append(segment_results[i])
                # 简单估算时间戳，每个片段30秒
                all_timestamps.append({
                    'start': i * 30,
                    'end': (i + 1) * 30
                })
            else:
                all_text.append("[无法识别的音频片段]")
                all_timestamps.append({
                    'start': i * 30,
                    'end': (i + 1) * 30
                })
                
            # 更新进度条
            if self.show_progress:
                text_prep_progress.update(
                    i + 1, 
                    f"处理片段 {i+1}/{len(segment_files)}"
                )
        
        # 完成文本准备进度条
        if self.show_progress:
            text_prep_progress.finish("文本片段处理完成")
        
        # 格式化文本以提高可读性
        if self.format_text:
            if self.show_progress:
                format_progress = ProgressBar(
                    total=1,
                    prefix="格式化文本",
                    suffix="处理中"
                )
            
            full_text = TextFormatter.format_segment_text(
                all_text, 
                timestamps=all_timestamps if self.include_timestamps else None,
                include_timestamps=self.include_timestamps,
                separate_segments=True  # 启用分片分隔
            )
            
            if self.show_progress:
                format_progress.finish("格式化完成")
        else:
            # 如果不格式化，仍使用原来的合并方式
            full_text = "\n\n".join([text for text in all_text if text and text != "[无法识别的音频片段]"])
        
        return full_text
    
    def process_single_file(self, input_path: str) -> bool:
        """
        处理单个音频文件
        
        Args:
            input_path: 音频文件路径
            
        Returns:
            处理是否成功
        """
        filename = os.path.basename(input_path)
        file_progress = None
        
        try:
            # 创建单个文件总进度条
            file_progress = self.create_progress_bar(
                "file_progress",
                total=4,  # 分割、识别、重试、保存 4个阶段
                prefix=f"处理 {filename}",
                suffix="准备中"
            )
            
            # 记录单个文件处理开始时间
            file_start_time = time.time()
            
            # 分割音频为较小片段
            self.update_progress("file_progress", 0, "分割音频")
            segment_files = self.split_audio_file(input_path)
            
            if not segment_files:
                logging.error(f"分割 {filename} 失败，跳过此文件")
                self.finish_progress("file_progress", "分割失败，跳过")
                return False
            
            # 处理音频片段
            self.update_progress("file_progress", 1, "识别音频")
            segment_results = self.process_audio_segments(segment_files)
            
            # 如果处理被中断，保存当前结果并退出
            if self.interrupt_received:
                logging.warning("处理被中断，尝试保存已完成的识别结果...")
                self.update_progress("file_progress", 2, "处理被中断")
            else:
                # 重试失败的片段
                self.update_progress("file_progress", 2, "重试失败片段")
                segment_results = self.retry_failed_segments(segment_files, segment_results)
            
            # 准备结果文本
            self.update_progress("file_progress", 3, "生成文本")
            full_text = self.prepare_result_text(segment_files, segment_results)
            
            # 保存结果到文件
            output_file = os.path.join(self.output_folder, filename.replace(".mp3", ".txt"))
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(full_text)
            
            # 统计识别结果
            success_count = len(segment_results)
            fail_count = len(segment_files) - success_count
            
            # 计算并显示单个文件处理时长
            file_duration = time.time() - file_start_time
            formatted_duration = format_time_duration(file_duration)
            
            status = "（部分完成 - 已中断）" if self.interrupt_received else ""
            
            # 更新已处理记录
            self.processed_files[input_path] = {
                "processed_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "output_file": output_file,
                "interrupted": self.interrupt_received,
                "success_rate": f"{success_count}/{len(segment_files)}",
                "duration": formatted_duration
            }
            self._save_processed_records()
            
            # 完成文件处理进度条
            success_rate = f"{success_count}/{len(segment_files)}"
            self.finish_progress("file_progress", f"完成 - 成功率: {success_rate}, 耗时: {formatted_duration}")
            
            logging.info(f"✅ {filename} 转换完成{status}: 成功识别 {success_count}/{len(segment_files)} 片段" + 
                      (f", 失败 {fail_count} 片段" if fail_count > 0 else "") + 
                      f" - 耗时: {formatted_duration}")
            
            return True
            
        except Exception as e:
            logging.error(f"❌ {filename} 处理失败: {str(e)}")
            # 确保进度条完成
            if file_progress:
                self.finish_progress("file_progress", f"处理失败: {str(e)}")
            return False
    
    def process_all_files(self) -> Tuple[int, float]:
        """
        处理所有MP3文件
        
        Returns:
            (处理文件数, 总耗时)
        """
        # 记录总体开始时间
        total_start_time = time.time()
        
        try:
            # 设置信号处理
            signal.signal(signal.SIGINT, self.handle_interrupt)
            
            # 检查网络连接
            try:
                logging.info("检查网络连接...")
                status_code = requests.get("https://www.google.com").status_code
                logging.info(f"网络连接状态: {status_code}")
            except Exception as e:
                logging.warning(f"网络连接检查失败: {str(e)}")
            
            # 获取所有MP3文件
            mp3_files = [f for f in os.listdir(self.mp3_folder) if f.endswith(".mp3")]
            unprocessed_files = []
            
            for filename in mp3_files:
                input_path = os.path.join(self.mp3_folder, filename)
                # 检查文件是否已处理
                if input_path not in self.processed_files:
                    unprocessed_files.append(input_path)
            
            # 创建总进度条
            if self.show_progress and unprocessed_files:
                self.overall_progress_bar = ProgressBar(
                    total=len(unprocessed_files),
                    prefix="总体进度",
                    suffix=f"0/{len(unprocessed_files)} 文件"
                )
            
            processed_files_count = 0
            
            # 遍历处理所有MP3文件
            for i, input_path in enumerate(unprocessed_files):
                if self.interrupt_received:
                    logging.warning("程序被用户中断，停止处理新文件。")
                    break
                
                filename = os.path.basename(input_path)
                
                # 更新总进度条
                if self.show_progress and self.overall_progress_bar:
                    self.overall_progress_bar.update(
                        processed_files_count,
                        f"{processed_files_count}/{len(unprocessed_files)} 文件处理中 - 当前: {filename}"
                    )
                
                # 处理单个文件
                success = self.process_single_file(input_path)
                if success:
                    processed_files_count += 1
                
                # 更新总进度条
                if self.show_progress and self.overall_progress_bar:
                    self.overall_progress_bar.update(
                        processed_files_count,
                        f"{processed_files_count}/{len(unprocessed_files)} 文件完成"
                    )
                
                if self.interrupt_received:
                    break
            
            # 完成总进度条
            if self.show_progress and unprocessed_files and self.overall_progress_bar:
                status = " (已中断)" if self.interrupt_received else " (全部完成)"
                self.overall_progress_bar.finish(f"{processed_files_count}/{len(unprocessed_files)} 文件完成{status}")
            
            # 计算总处理时长
            total_duration = time.time() - total_start_time
            
            # 所有识别完成后，显示服务使用统计
            self.print_statistics(processed_files_count, total_duration)
            
            return processed_files_count, total_duration
            
        finally:
            # 恢复原始信号处理程序
            signal.signal(signal.SIGINT, self.original_sigint_handler)
            
            # 清理临时文件
            self.cleanup()
    
    def print_statistics(self, processed_files_count: int, total_duration: float):
        """打印处理统计信息"""
        formatted_total_duration = format_time_duration(total_duration)
        
        # 显示ASR服务统计
        stats = self.asr_manager.get_service_stats()
        logging.info("\nASR服务使用统计:")
        for name, stat in stats.items():
            logging.info(f"  {name}: 使用次数 {stat['count']}, 成功率 {stat['success_rate']}, " +
                      f"可用状态: {'可用' if stat['available'] else '禁用'}")
                
        # 打印总结信息
        logging.info(f"\n总结: 处理了 {processed_files_count} 个文件, 总耗时: {formatted_total_duration}")
        
        # 显示平均每个文件处理时长
        if processed_files_count > 0:
            avg_time = total_duration / processed_files_count
            formatted_avg_time = format_time_duration(avg_time)
            logging.info(f"平均每个文件处理时长: {formatted_avg_time}")
    
    def cleanup(self):
        """清理临时文件和资源"""
        try:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            logging.info(f"✓ 临时文件已清理: {self.temp_dir}")
        except Exception as e:
            logging.warning(f"⚠️ 清理临时文件失败: {str(e)}")
        
        if self.interrupt_received:
            logging.info("\n程序已安全终止，已保存处理进度。您可以稍后继续处理剩余文件。")
# No backticks found in the code. If the error persists, manually replace them with repr()
# Example: If you have `x`, replace it with repr(x)
