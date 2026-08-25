from typing import overload, List

class script:
	def __init__(self, name):
		self.file = open(name + ".xml", "w")
		self.opentags = []
		self.open("xflscript version=\"1.0\"", True)
		self.foilOpen = False
		self.metaOpen = False
		self.planeAnalysisOpen = False
		self.aCount = -1
		self.checks = [self.foilOpen, self.metaOpen, self.planeAnalysisOpen]
		self.closeOuts = [self.foilClose, self.metaClose]

	def open(self, tag, nl=True):
		for i in range(len(self.opentags)):
			self.file.write("\t")
		self.opentags.append(tag)
		self.file.write("<" + tag + ">" + ("" if not nl else "\n"))

	def close(self, wt=True):
		popped = self.opentags.pop()
		if wt:
			for i in range(len(self.opentags)):
				self.file.write("\t")
		self.file.write("</" + popped + ">\n")

	def oneline(self, tag, content):
		self.open(tag, nl=False)
		if type(content) == list:
			for i in content:
				self.file.write(str(i))
				if i != content[-1]:
					self.file.write(", ")
		else:
			self.file.write(str(content))
		self.close(wt=False)

	def runChecks(self, exceptions):
		if type(exceptions) == list:
			for i in exceptions:
				if not i:
					raise Exception("Please enter the proper scope prior to running this function.")
		elif exceptions:
			return
		self.runChecksNoEx()

	def runChecksNoEx(self):
		for i in self.checks:
			if i:
				raise Exception("Please close out of limiting scopes prior to running this function.")

	def metadata(self, project_make=True, project_name=None, project_load=None, output_format="CSV"):
		self.runChecksNoEx()
		self.metaOpen = True
		self.open("Metadata")
		if project_make:
			self.oneline("make_Project_File", project_make)
		if project_name:
			self.oneline("Project_File_Name", project_name)
		if project_load:
			self.oneline("Load_Project_File", project_load)
		self.oneline("Polar_Text_Output_Format", output_format)

	def directories(self, output_dir=None, foils_dir=None, analysis_xml_dir=None, foil_polars_dir=None, plane_analysis_dir=None, plane_def_dir=None, recursive_checking=False):
		self.runChecks(self.metaOpen)
		self.open('Directories')
		if output_dir:
			self.oneline('Output_Dir', output_dir)
		if foils_dir:
			self.oneline('Foil_Files_dir', foils_dir)
		if analysis_xml_dir:
			self.oneline('Foil_Analysis_xml_dir', analysis_xml_dir)
		if foil_polars_dir:
			self.oneline('Foil_Polars_dir', foil_polars_dir)
		if plane_def_dir:
			self.oneline('Plane_Definition_xml_dir', plane_def_dir)
		if plane_analysis_dir:
			self.oneline("Plane_Analysis_xml_dir", plane_analysis_dir)
		self.oneline('Recursive_Checking', recursive_checking)
		self.close()

	def multithreading(self, enabled=True, priority="Normal", max_threads=1):
		self.runChecks(self.metaOpen)
		self.open('MultiThreading')
		self.oneline('Allow_Multithreading', enabled)
		self.oneline('Thread_Priority', priority)
		self.oneline('Max_threads', max_threads)
		self.close()

	def metaClose(self):
		self.runChecks(self.metaOpen)
		self.metaOpen = False
		self.close()

	def foil_analysis(self, foil_files=None, process_all=False, analysis_files=None, polar_type=1, top_transition=1.0,
				 bottom_transition=1.0):
		self.runChecksNoEx()
		self.open('Foil_Analysis')
		self.foilOpen = True
		if foil_files:
			self.open('Foil_Files')
			for foil in foil_files:
				self.oneline('Foil_File_Name', foil)
			self.close()
		self.open('Analysis_Files')
		self.oneline('Process_All_Files', process_all)
		if analysis_files:
			for af in analysis_files:
				self.oneline('Analysis_File_Name', af)
		self.close()

	def batched_foil_analysis(self, polar_type=1, top_transition=1.0, bottom_transition=1.0, Alpha=None, Reynolds=None, Mach=None, NCrit=None):
		self.runChecks(self.foilOpen)
		self.open('Batch_Analysis_Data')
		self.oneline('Polar_Type', polar_type)
		self.oneline('Forced_Top_Transition', top_transition)
		self.oneline('Forced_Bottom_Transition', bottom_transition)
		self.open('Batch_Range')
		valueDict = {'Alpha': Alpha, 'Reynolds': Reynolds, 'Mach': Mach, 'NCrit': NCrit}
		for i in valueDict.keys():
			if valueDict[i] is None:
				continue
			self.oneline(i, ", ".join(map(str, valueDict[i])))
		self.close()
		self.close()
		
	def foil_oppoint_range(self, Alpha=None, Cl=None, Reynolds=None, Spec_Alpha=True, From_Zero=True):
		self.runChecks(self.foilOpen)
		self.open('OpPoint_Range')
		if Alpha:
			self.oneline('Alpha', f"{Alpha[0]}, {Alpha[1]}, {Alpha[2]}")
		if Cl:
			self.oneline('Cl', f"{Cl[0]}, {Cl[1]}, {Cl[2]}")
		if Reynolds:
			self.oneline('Reynolds', ", ".join(map(str, Reynolds)))
		self.oneline('Spec_Alpha', Spec_Alpha)
		self.oneline('From_Zero', From_Zero)
		self.close()


	def foil_output(self, make_polars_bin=False, make_polars_text=False, make_oppoints=False):
		self.runChecks(self.foilOpen)
		self.open('Output')
		self.oneline('Make_Polars_bin_file', make_polars_bin)
		self.oneline('Make_Polars_text_file', make_polars_text)
		self.oneline('Make_Oppoints', make_oppoints)
		self.close()

	def foil_options(self, max_iterations=100, repanel=False, panels=150):
		self.runChecks(self.foilOpen)
		self.open('Options')
		self.oneline('Max_XFoil_Iterations', max_iterations)
		self.oneline('Repanel_Foils', repanel)
		self.oneline('Foil_Panels', panels)
		self.close()

	def foilClose(self):
		self.runChecks(self.foilOpen)
		self.foilOpen = False
		self.close()

	def plane_analysis(self):
		"""Starts a plane analysis section"""
		self.runChecksNoEx()
		self.planeAnalysisOpen = True
		self.open('Plane_Analysis')

	def plane_analysis_output(self, make_oppoints=True, make_oppoints_text=True,
							export_cp=False, make_polars_text=True):
		"""Configure plane analysis output settings"""
		self.runChecks(self.planeAnalysisOpen)
		self.open('Plane_Analysis_Output')
		self.oneline('make_oppoints', make_oppoints)
		self.oneline('make_oppoints_text_file', make_oppoints_text)
		self.oneline('export_oppoint_Cp', export_cp)
		self.oneline('make_polars_text_file', make_polars_text)
		self.close()

	def add_foil_files(self, foil_files: List[str]):
		"""Add foil .dat files to be loaded for analysis"""
		self.runChecks(self.planeAnalysisOpen)
		self.open('Foil_Dat_Files')
		for foil in foil_files:
			self.oneline('Foil_File_Name', foil)
		self.close()

	def add_polar_files(self, polar_files: List[str]):
		"""Add polar .plr files to be loaded for analysis"""
		self.runChecks(self.planeAnalysisOpen)
		self.open('Foil_Polar_Files')
		for polar in polar_files:
			self.oneline('Polar_File_Name', polar)
		self.close()

	def plane_definition_files(self, process_all=False, plane_files: List[str] = None):
		"""Configure plane definition files to load"""
		self.runChecks(self.planeAnalysisOpen)
		self.open('Plane_Definition_Files')
		self.oneline('Process_All_Files', process_all)
		if plane_files:
			for pf in plane_files:
				self.oneline('Plane_File_Name', pf)
		self.close()

	def plane_analysis_files(self, process_all=False, analysis_files: List[str] = None):
		"""Configure plane analysis files to load"""
		self.runChecks(self.planeAnalysisOpen)
		self.open('Plane_Analysis_Files')
		self.oneline('Process_All_Files', process_all)
		if analysis_files:
			for af in analysis_files:
				self.oneline('Analysis_File_Name', af)
		self.close()

	def plane_analysis_data(self, analysis_count):
		"""Start plane analysis data section. Analysis count is how many different any analyses will be run,
		after which the script will automatically close out the section."""
		self.aCount = analysis_count
		self.runChecks(self.planeAnalysisOpen)
		self.open('Plane_Analysis_Data')


	def add_range(self, range_type: int, ranges: List[float]):
		"""Add analysis range parameters
		range_type: One of 'T12', 'T3', 'T5', 'T6', 'T7', 'T8'
		ranges: List of strings with format "min, max, increment"
		"""
		self.runChecks(self.planeAnalysisOpen)
		if self.aCount < 1:
			raise Exception("Please start plane analysis data section before adding ranges. (Either that or you have set your analysis count too low.)")
		tag = f"T{range_type}_Range"
		for r in ranges:
			self.oneline(tag, r)
		if self.aCount > 1:
			self.aCount -= 1
		if self.aCount == 1:
			self.close()


	def viscous_loop(self, enable=False, init_virtual_twist=False,
					relax_factor=0.5, alpha_precision=0.01, max_iterations=30):
		"""Configure viscous loop settings"""
		self.runChecks(self.planeAnalysisOpen)
		self.open('Viscous_Loop')
		self.oneline('Enable', enable)
		self.oneline('Init_Virtual_Twist', init_virtual_twist)
		self.oneline('Relax_Factor', relax_factor)
		self.oneline('Alpha_Precision', alpha_precision)
		self.oneline('Max_Iterations', max_iterations)
		self.close()

	def finish(self):
		while len(self.opentags) > 1:
			self.close()
		self.file.write("</xflscript>")
		self.file.close()

	def __del__(self):
		for i in self.checks:
			if i:
				self.closeOuts[i]()
		self.finish()